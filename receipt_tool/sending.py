"""Explicit live operations, durable claims and final-status tracking."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

from .core import InputError, credentials_for

API_URL = "https://api.cloudpayments.ru"
SCHEMA_VERSION = "cloudkassir-corrections/1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


class State:
    def __init__(self, path: Path, *, create=False):
        path = path.resolve()
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation prevents replacing an existing journal.
            try:
                with path.open("xb"):
                    pass
            except FileExistsError:
                raise InputError("База уже существует. Инициализация не заменяет существующую базу.") from None
        elif not path.is_file():
            raise InputError("База отправок не найдена. Восстановите её; для новой установки есть init-state.")
        self.connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30)
        self.connection.row_factory = sqlite3.Row
        if create:
            self.connection.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE receipts (
                    receipt_key TEXT PRIMARY KEY,
                    entity_id TEXT NOT NULL,
                    receipt_id TEXT NOT NULL,
                    receipt_type TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    payload_hash TEXT NOT NULL,
                    account_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cloud_receipt_id TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_id, receipt_id)
                );
            """)
            self.connection.executemany("INSERT INTO metadata VALUES (?, ?)", [
                ("schema", SCHEMA_VERSION), ("installation_id", str(uuid.uuid4())),
            ])
            self.connection.commit()
        try:
            row = self.connection.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
        except sqlite3.DatabaseError:
            self.connection.close()
            raise InputError("Неизвестный формат базы. Автоматическая замена или миграция запрещена.") from None
        if not row or row[0] != SCHEMA_VERSION:
            self.connection.close()
            raise InputError("Версия базы не поддерживается.")

    def close(self):
        self.connection.close()

    def processed_count(self, entity_id: str) -> int:
        return self.connection.execute("SELECT count(*) FROM receipts WHERE entity_id=? AND status='processed'", (entity_id,)).fetchone()[0]

    def claim(self, preview: dict, account: str) -> str | None:
        """Commit a claim before HTTP; concurrent processes cannot both claim."""
        con = self.connection
        con.execute("BEGIN IMMEDIATE")
        try:
            existing = con.execute("SELECT * FROM receipts WHERE entity_id=? AND receipt_id=?", (preview["entity_id"], preview["receipt_id"])).fetchone()
            payload_hash = digest(preview["payload"])
            if existing:
                if (existing["payload_hash"], existing["account_hash"], existing["request_id"]) != (payload_hash, account, preview["request_id"]):
                    raise InputError("Для ранее учтённого чека изменились состав, тип, реквизиты или API-аккаунт. Нужна отдельная сверка.")
                con.commit()
                return existing["status"]
            other = con.execute("SELECT account_hash FROM receipts WHERE entity_id=? LIMIT 1", (preview["entity_id"],)).fetchone()
            if other and other[0] != account:
                raise InputError("Нельзя менять API-аккаунт существующего entity_id без отдельной миграции.")
            stamp = now()
            con.execute("""INSERT INTO receipts (
                receipt_key, entity_id, receipt_id, receipt_type, order_id, request_id,
                payload_hash, account_hash, status, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (preview["receipt_key"], preview["entity_id"], preview["receipt_id"], preview["receipt_type"], preview["order_id"], preview["request_id"], payload_hash, account, "sending", stamp, stamp))
            con.commit()
            return None
        except BaseException:
            con.rollback()
            raise

    def update(self, key: str, status: str, cloud_id: str | None = None, note=""):
        with self.connection:
            self.connection.execute("UPDATE receipts SET status=?, cloud_receipt_id=COALESCE(?,cloud_receipt_id), note=?, updated_at=? WHERE receipt_key=?",
                                    (status, cloud_id, note[:500], now(), key))

    def rows(self, entity: str | None = None) -> list[dict]:
        sql = "SELECT * FROM receipts"
        params = ()
        if entity:
            sql += " WHERE entity_id=?"
            params = (entity,)
        return [dict(row) for row in self.connection.execute(sql + " ORDER BY entity_id, receipt_id", params)]


class Client:
    def __init__(self, organization: dict):
        public_id, secret = credentials_for(organization)
        self.account_hash = digest(public_id)
        self._redactions = (public_id, secret)
        self.session = requests.Session()
        self.session.auth = (public_id, secret)
        # Financial operations must not use hidden retry adapters or redirects.
        self.session.headers.update({"Content-Type": "application/json"})

    def _post(self, path: str, payload: dict, request_id: str | None = None) -> dict:
        response = self.session.post(
            API_URL + path, json=payload,
            headers={"X-Request-ID": request_id} if request_id else {},
            timeout=(10, 90), allow_redirects=False,
        )
        if response.status_code != 200:
            raise InputError("CloudKassir вернул HTTP-ошибку; результат требует проверки.")
        result = response.json()
        if not isinstance(result, dict):
            raise InputError("CloudKassir вернул неожиданный формат ответа.")
        return result

    def safe_message(self, value) -> str:
        text = str(value or "")
        for secret in self._redactions:
            text = text.replace(secret, "[redacted]")
        return text[:500]

    def send(self, preview: dict) -> dict:
        return self._post("/kkt/correction-receipt", preview["payload"], preview["request_id"])

    def status(self, cloud_id: str) -> dict:
        return self._post("/kkt/correction-receipt/status/get", {"Id": cloud_id})


def ensure_live_organization(organization: dict):
    fiscal = organization["fiscal"]
    if organization.get("demo", True) is not False or organization.get("fiscal_confirmed") is not True:
        raise InputError("Отправка запрещена: нужны demo=false и fiscal_confirmed=true после проверки реквизитов.")
    if not fiscal["organization_inn"].strip("0") or fiscal["correction_number"].upper().startswith(("DEMO", "DRY-RUN")):
        raise InputError("Демонстрационные реквизиты запрещены для отправки.")
    credentials_for(organization)


def send_batch(previews: list[dict], organizations: dict, state: State, *, delay=2.0, client_factory=Client) -> dict:
    clients = {}
    selected = {}
    # Validate every selected entity before the first request.
    for preview in previews:
        entity = preview["entity_id"]
        ensure_live_organization(organizations[entity])
        selected[entity] = selected.get(entity, 0) + 1
    for entity, count in selected.items():
        if count > 1 and state.processed_count(entity) == 0:
            raise InputError("Для новой организации сначала отправьте один чек и дождитесь processed.")
        clients[entity] = client_factory(organizations[entity])
    counts = {"queued": 0, "skipped": 0, "error": 0, "unknown": 0}
    for preview in previews:
        client = clients[preview["entity_id"]]
        if state.claim(preview, client.account_hash) is not None:
            counts["skipped"] += 1
            continue
        try:
            result = client.send(preview)
            if result.get("Success") is False:
                state.update(preview["receipt_key"], "error", note=client.safe_message(result.get("Message")))
                counts["error"] += 1
                # A business rejection stops the batch before more fiscal operations.
                break
            model = result.get("Model")
            cloud_id = model.get("Id") if isinstance(model, dict) else result.get("Id")
            if result.get("Success") is not True or not isinstance(cloud_id, str) or not cloud_id:
                raise InputError("Ответ отправки не содержит подтверждённый идентификатор.")
            state.update(preview["receipt_key"], "queued", cloud_id)
            counts["queued"] += 1
        except Exception:
            # The service may have accepted the request before a timeout.
            # Never automatically retry, even after a restart or timeout expiry.
            state.update(preview["receipt_key"], "unknown", note="Неопределённый результат запроса. Не повторять; сверить с CloudKassir.")
            counts["unknown"] += 1
            break
        if delay:
            time.sleep(delay)
    return counts


def refresh_statuses(organizations: dict, state: State, entity: str | None = None, *, client_factory=Client):
    clients = {}
    rows = [r for r in state.rows(entity) if r["status"] in {"queued", "delayed", "sending", "unknown"} and r["cloud_receipt_id"]]
    for row in rows:
        entity_id = row["entity_id"]
        if entity_id not in organizations:
            raise InputError("В журнале есть организация, отсутствующая в конфигурации.")
        if entity_id not in clients:
            client = client_factory(organizations[entity_id])
            if client.account_hash != row["account_hash"]:
                raise InputError("API-аккаунт организации не совпадает с журналом.")
            clients[entity_id] = client
        client = clients[entity_id]
        if client.account_hash != row["account_hash"]:
            raise InputError("API-аккаунт организации не совпадает с журналом.")
        try:
            result = client.status(row["cloud_receipt_id"])
        except Exception:
            raise InputError("Не удалось получить статус. Сохранённые статусы не изменены для этого чека.") from None
        model = str(result.get("Model", "")).lower()
        if result.get("Success") is not True or model not in {"processed", "error", "queued"}:
            raise InputError("Неизвестный ответ статуса. Чек не считается завершённым.")
        state.update(row["receipt_key"], model, note=client.safe_message(result.get("Message")))
    return state.rows(entity)


def poll_statuses(organizations: dict, state: State, entity: str | None, timeout: int, interval: int):
    deadline = time.monotonic() + timeout
    while True:
        rows = refresh_statuses(organizations, state, entity)
        pending = [r for r in rows if r["status"] in {"sending", "queued", "delayed"} and r["cloud_receipt_id"]]
        if not pending:
            return rows
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            for row in pending:
                state.update(row["receipt_key"], "delayed", note="Не получен конечный статус за время ожидания. Не отправлять повторно.")
            return state.rows(entity)
        time.sleep(min(interval, remaining))
