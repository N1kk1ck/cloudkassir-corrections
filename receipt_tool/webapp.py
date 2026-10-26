"""Local-only offline preparation UI. No sending endpoint is provided."""
from __future__ import annotations

import secrets
import threading
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for

from .core import InputError, load_inputs, write_preparation

# Installed packages may live in a read-only site-packages directory.
# Inputs, uploads and reports belong to the chosen working directory.
ROOT = Path.cwd()


def create_app(root: Path = ROOT) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_bytes(32)
    app.config.update(MAX_CONTENT_LENGTH=100 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")
    lock = threading.Lock()

    @app.before_request
    def local_only():
        if request.host.split(":")[0] not in {"127.0.0.1", "localhost"}:
            abort(403)
        if request.method == "POST":
            if not secrets.compare_digest(str(session.get("csrf", "")), str(request.form.get("csrf", ""))) or not session.get("csrf"):
                abort(403)

    @app.get("/")
    def index():
        session.setdefault("csrf", secrets.token_urlsafe(32))
        return render_template("index.html", summary=None, csrf=session["csrf"])

    @app.post("/prepare")
    def prepare_files():
        if not lock.acquire(blocking=False):
            flash("Другая подготовка уже выполняется. Дождитесь её окончания.")
            return redirect(url_for("index"))
        try:
            # New random workspace for each upload; never overwrite previous data.
            workspace = root / "runtime" / "uploads" / secrets.token_hex(16)
            workspace.mkdir(parents=True)
            paths = {}
            for field in ["config", "receipts", "items", "mapping"]:
                uploaded = request.files.get(field)
                if uploaded is None or not uploaded.filename:
                    if field == "mapping":
                        continue
                    raise InputError("Нужно выбрать организации, список чеков и состав заказов.")
                suffix = Path(uploaded.filename).suffix.lower()
                allowed = {".yaml", ".yml"} if field in {"config", "mapping"} else {".xlsx", ".csv"}
                if suffix not in allowed:
                    raise InputError("Организации и соответствие колонок: YAML; таблицы: CSV/XLSX.")
                paths[field] = workspace / (field + suffix)
                uploaded.save(paths[field])
            _, prepared = load_inputs(paths["config"], paths["receipts"], paths["items"], paths.get("mapping"))
            output = workspace / "reports"
            summary = write_preparation(output, prepared)
            session["report_dir"] = str(output.relative_to(root))
            return render_template("index.html", summary=summary, csrf=session["csrf"])
        except InputError as exc:
            flash(str(exc))
            return redirect(url_for("index"))
        except OSError:
            flash("Не удалось сохранить файлы или отчёт. Проверьте свободное место и права.")
            return redirect(url_for("index"))
        finally:
            lock.release()

    @app.get("/reports/<name>")
    def download(name: str):
        if name not in {"ready.csv", "review.csv", "payload_preview.jsonl", "summary.json"} or not session.get("report_dir"):
            abort(404)
        directory = (root / session["report_dir"]).resolve()
        if not directory.is_relative_to((root / "runtime" / "uploads").resolve()):
            abort(404)
        path = directory / name
        if not path.is_file():
            abort(404)
        return send_file(path, as_attachment=True)

    return app


def run():
    create_app().run(host="127.0.0.1", port=8765, debug=False)
