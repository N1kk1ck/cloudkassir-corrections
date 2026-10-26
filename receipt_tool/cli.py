"""Command-line entry points. Default operations are offline."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

from .core import InputError, load_inputs, load_organizations, write_csv, write_preparation
from .sending import State, poll_statuses, refresh_statuses, send_batch


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="CloudKassir: подготовка и контролируемая отправка чеков коррекции.")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ["prepare", "send"]:
        cmd = commands.add_parser(name, help="Offline-проверка и preview" if name == "prepare" else "Явная отправка проверенных чеков")
        cmd.add_argument("--config", type=Path, default=Path("config/organizations.yaml"))
        cmd.add_argument("--receipts", type=Path, required=True)
        cmd.add_argument("--items", type=Path, required=True)
        cmd.add_argument("--mapping", type=Path)
        if name == "prepare":
            cmd.add_argument("--output", type=Path, default=Path("output"))
        else:
            cmd.add_argument("--send", action="store_true", help="Обязательное разрешение отправки")
            cmd.add_argument("--confirm", default="")
            cmd.add_argument("--limit", type=int, help="Максимум новых отправок; уже учтённые чеки пропускаются")
            cmd.add_argument("--entity")
            cmd.add_argument("--delay", type=float, default=2.0)
            cmd.add_argument("--state", type=Path, default=Path("runtime/state.db"))
    init = commands.add_parser("init-state", help="Создать журнал только для новой установки")
    init.add_argument("--state", type=Path, default=Path("runtime/state.db"))
    init.add_argument("--confirm", default="")
    status = commands.add_parser("status", help="Обновить статусы через API без повторной отправки")
    status.add_argument("--config", type=Path, default=Path("config/organizations.yaml"))
    status.add_argument("--state", type=Path, default=Path("runtime/state.db"))
    status.add_argument("--entity")
    status.add_argument("--poll", action="store_true")
    status.add_argument("--timeout", type=int, default=600)
    status.add_argument("--interval", type=int, default=30)
    report = commands.add_parser("report", help="Экспортировать локальный журнал без API")
    report.add_argument("--state", type=Path, default=Path("runtime/state.db"))
    report.add_argument("--output", type=Path, default=Path("output/sending_report.csv"))
    report.add_argument("--entity")
    commands.add_parser("web", help="Открыть локальный интерфейс подготовки")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    state = None
    try:
        if args.command == "web":
            from .webapp import run
            run()
            return 0
        if args.command == "init-state":
            if args.confirm != "NEW_INSTALLATION":
                raise InputError("Новая база требует --confirm NEW_INSTALLATION. Для продолжения работы восстановите прежнюю базу.")
            State(args.state, create=True).close()
            print("Создан журнал новой установки.")
            return 0
        if args.command == "send":
            if not args.send or args.confirm != "SEND_CORRECTIONS" or args.limit is None or args.limit <= 0:
                raise InputError("Нужны --send --confirm SEND_CORRECTIONS и положительный --limit.")
            if not math.isfinite(args.delay) or args.delay < 0:
                raise InputError("--delay должен быть конечным неотрицательным числом.")
        if args.command in {"prepare", "send"}:
            organizations, prepared = load_inputs(args.config, args.receipts, args.items, args.mapping)
            if args.command == "prepare":
                generated = {(args.output / name).resolve() for name in ["payload_preview.jsonl", "payload_preview.tmp", "ready.csv", "review.csv", "summary.json"]}
                sources = {p.resolve() for p in [args.config, args.receipts, args.items, args.mapping] if p is not None}
                if generated & sources:
                    raise InputError("Папка отчётов привела бы к перезаписи входного файла. Выберите другую --output.")
                print(json.dumps(write_preparation(args.output, prepared), ensure_ascii=False))
                print("OFFLINE: API-запросы не выполнялись.")
                return 2 if prepared.review else 0
            if args.entity and args.entity not in organizations:
                raise InputError("Указанный entity_id отсутствует в конфигурации.")
            reviews = [r for r in prepared.review if not args.entity or r["entity_id"] == args.entity]
            if reviews:
                raise InputError("В выбранной организации есть чеки на проверке. Исправьте данные или передайте отдельный проверенный список.")
            available = sorted([r for r in prepared.ready if not args.entity or r["entity_id"] == args.entity], key=lambda r: (r["entity_id"], r["receipt_id"]))
            if not available:
                raise InputError("Не выбраны готовые чеки.")
            state = State(args.state)
            existing = {(r["entity_id"], r["receipt_id"]): r for r in state.rows()}
            selected = [r for r in available if (r["entity_id"], r["receipt_id"]) not in existing][:args.limit]
            if not selected:
                print("Новых чеков нет. Учтённые чеки автоматически не повторяются.")
                return 0 if all(existing[(r["entity_id"], r["receipt_id"])]["status"] == "processed" for r in available) else 3
            print(json.dumps(send_batch(selected, organizations, state, delay=args.delay), ensure_ascii=False))
            rows = {r["receipt_key"]: r for r in state.rows()}
            # Acceptance is not fiscal completion, including when all rows were skipped.
            return 0 if all(rows[r["receipt_key"]]["status"] == "processed" for r in selected) else 3
        state = State(args.state)
        if args.command == "report":
            if args.output.suffix.lower() != ".csv" or args.output.resolve() == args.state.resolve():
                raise InputError("Отчёт должен быть отдельным CSV-файлом и не заменять базу.")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            write_csv(args.output, ["entity_id", "receipt_id", "order_id", "receipt_type", "request_id", "status", "cloud_receipt_id", "note", "created_at", "updated_at"], state.rows(args.entity))
            print("Локальный отчёт сохранён. API-запросы не выполнялись.")
            return 0
        if args.timeout <= 0 or args.interval <= 0:
            raise InputError("--timeout и --interval должны быть положительными.")
        organizations = load_organizations(args.config)
        if args.entity and args.entity not in organizations:
            raise InputError("Указанный entity_id отсутствует в конфигурации.")
        rows = poll_statuses(organizations, state, args.entity, args.timeout, args.interval) if args.poll else refresh_statuses(organizations, state, args.entity)
        counts = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        print(json.dumps(counts, ensure_ascii=False))
        return 0 if all(r["status"] == "processed" for r in rows) else 3
    except InputError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        print("Ошибка доступа к файлам или журналу. Не заменяйте рабочую базу новой.", file=sys.stderr)
        return 2
    finally:
        if state is not None:
            state.close()


if __name__ == "__main__":
    raise SystemExit(main())
