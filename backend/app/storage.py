"""SQLite persistence for tasks and settings.

Two physically separate databases are used: real tasks (``agent.db``) and
demonstration tasks (``demo.db``).  Demo results can therefore never leak into
the real task history, even through a coding mistake in a filter.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH, DEMO_DB_PATH, SECRETS_PATH, STAGE_DEFINITIONS


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'real',
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    source_name TEXT,
    source_sha256 TEXT,
    size_bytes INTEGER,
    slide_count INTEGER,
    demo_scenario TEXT,
    options_json TEXT NOT NULL DEFAULT '{}',
    document_json TEXT,
    stages_json TEXT NOT NULL DEFAULT '[]',
    summary_json TEXT,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    api_snapshot_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def default_stages() -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "label": label,
            "status": "pending",
            "message": "",
            "progress": 0.0,
            "started_at": None,
            "finished_at": None,
            "detail": {},
        }
        for key, label in STAGE_DEFINITIONS
    ]


DEFAULT_SETTINGS: dict[str, Any] = {
    "api_base_url": "",
    "api_model": "",
    "api_timeout_s": 60,
    "api_max_retries": 1,
    "api_max_concurrency": 2,
    "api_temperature": 0.2,
    "max_calls_per_task": 8,
    "allow_private_base_url": False,
    "allow_insecure_base_url": False,
    "response_format_json": True,
    "vision_verified": False,
    "last_test": None,
}


class TaskStore:
    def __init__(self, db_path: Path, mode: str) -> None:
        self.db_path = Path(db_path)
        self.mode = mode
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ------------------------------------------------------------------ tasks
    def create(self, *, name: str, options: dict, source_name: str, size_bytes: int,
               demo_scenario: str | None = None, api_snapshot: dict | None = None) -> dict:
        task_id = new_id()
        ts = now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO tasks (id, mode, name, status, progress, source_name, size_bytes,
                       demo_scenario, options_json, stages_json, api_snapshot_json,
                       created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id, self.mode, name, "queued", 0.0, source_name, size_bytes,
                    demo_scenario, json.dumps(options, ensure_ascii=False),
                    json.dumps(default_stages(), ensure_ascii=False),
                    json.dumps(api_snapshot or {}, ensure_ascii=False), ts, ts,
                ),
            )
        return self.get(task_id)  # type: ignore[return-value]

    def get(self, task_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def list(self, limit: int = 30, offset: int = 0) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_to_task(r, summary_only=True) for r in rows]

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])

    def update(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "name", "status", "progress", "source_name", "source_sha256", "size_bytes",
            "slide_count", "document_json", "stages_json", "summary_json", "warnings_json",
            "error", "started_at", "finished_at", "api_snapshot_json", "options_json",
        }
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"field not updatable: {key}")
            sets.append(f"{key} = ?")
            values.append(json.dumps(value, ensure_ascii=False) if key.endswith("_json") else value)
        sets.append("updated_at = ?")
        values.append(now_iso())
        values.append(task_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", values)

    def patch_stage(self, task_id: str, stage_key: str, **fields: Any) -> None:
        with self._lock:
            task = self.get(task_id)
            if not task:
                return
            stages = task["stages"]
            for stage in stages:
                if stage["key"] == stage_key:
                    stage.update(fields)
                    break
            self.update(task_id, stages_json=stages)

    def set_stage_bulk(self, task_id: str, stages: list[dict]) -> None:
        self.update(task_id, stages_json=stages)

    def delete(self, task_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def _row_to_task(self, row: sqlite3.Row, summary_only: bool = False) -> dict:
        task = {
            "id": row["id"],
            "mode": row["mode"],
            "name": row["name"],
            "status": row["status"],
            "progress": row["progress"],
            "source_name": row["source_name"],
            "source_sha256": row["source_sha256"],
            "size_bytes": row["size_bytes"],
            "slide_count": row["slide_count"],
            "demo_scenario": row["demo_scenario"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error": row["error"],
            "options": json.loads(row["options_json"] or "{}"),
            "stages": json.loads(row["stages_json"] or "[]"),
            "warnings": json.loads(row["warnings_json"] or "[]"),
            "summary": json.loads(row["summary_json"]) if row["summary_json"] else None,
        }
        if not summary_only:
            task["document"] = json.loads(row["document_json"]) if row["document_json"] else None
            task["api_snapshot"] = json.loads(row["api_snapshot_json"] or "{}")
        return task


class SettingsStore:
    """Small key/value store for non-secret options and the API key."""

    def __init__(self, db_path: Path, secrets_path: Path) -> None:
        self.db_path = Path(db_path)
        self.secrets_path = Path(secrets_path)
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def all(self) -> dict[str, Any]:
        values = dict(DEFAULT_SETTINGS)
        with self._connect() as conn:
            for row in conn.execute("SELECT key, value FROM settings"):
                try:
                    values[row["key"]] = json.loads(row["value"])
                except json.JSONDecodeError:
                    values[row["key"]] = row["value"]
        return values

    def save(self, values: dict[str, Any]) -> dict[str, Any]:
        ts = now_iso()
        with self._lock, self._connect() as conn:
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (key, json.dumps(value, ensure_ascii=False), ts),
                )
        return self.all()

    # ---------------------------------------------------------------- secrets
    def _read_secrets(self) -> dict[str, Any]:
        if not self.secrets_path.exists():
            return {}
        try:
            return json.loads(self.secrets_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def get_api_key(self) -> str:
        return str(self._read_secrets().get("api_key") or "")

    def set_api_key(self, key: str) -> None:
        data = self._read_secrets()
        if key:
            data["api_key"] = key
        else:
            data.pop("api_key", None)
        self.secrets_path.parent.mkdir(parents=True, exist_ok=True)
        self.secrets_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:  # best effort on Windows; POSIX honours it
            self.secrets_path.chmod(0o600)
        except OSError:
            pass

    def masked_key(self) -> str:
        key = self.get_api_key()
        if not key:
            return ""
        if len(key) <= 8:
            return "••••"
        return f"{key[:3]}••••••{key[-4:]}"


real_store = TaskStore(DB_PATH, "real")
demo_store = TaskStore(DEMO_DB_PATH, "demo")
settings_store = SettingsStore(DB_PATH, SECRETS_PATH)


def store_for(mode: str) -> TaskStore:
    return demo_store if mode == "demo" else real_store
