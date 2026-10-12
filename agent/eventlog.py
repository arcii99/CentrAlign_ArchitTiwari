"""The run record: an append-only SQLite event log, which is also the audit trail and the evidence.

    runs       one row per run (request, model, status, final summary, latest state snapshot)
    events     append-only: everything that happened, in order (never updated or deleted)
    llm_calls  the exact messages sent to the model and its raw reply, for debugging prompts

Per-run files (attachments, downloads, facts.json, report) live in <runs_dir>/<run_id>/.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, request TEXT NOT NULL, requester TEXT, model TEXT, status TEXT NOT NULL,
    started_at TEXT NOT NULL, ended_at TEXT, summary TEXT, state TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, seq INTEGER NOT NULL, at TEXT NOT NULL,
    kind TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events (run_id, seq);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, seq INTEGER NOT NULL, request TEXT NOT NULL,
    response TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_append_only_u BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_append_only_d BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _default(obj):
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return str(obj)


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_default)


class EventLog:
    def __init__(self, runs_dir: Path):
        self.runs_dir = runs_dir
        runs_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(runs_dir / "runs.db", isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(runs)")}
        for col in ("snapshot", "heartbeat_at", "verdict"):  # added in Phase 2; older logs get them on open
            if col not in have:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT")
        self._seq: dict[str, int] = {}
        self._listeners: list[Callable[[str, str, dict], None]] = []

    def subscribe(self, fn: Callable[[str, str, dict], None]) -> None:
        """fn(run_id, kind, data) is called for every event, e.g. to show progress in a terminal."""
        self._listeners.append(fn)

    def new_run(self, request: str, requester: str, model: str) -> str:
        run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        self.db.execute("INSERT INTO runs (id, request, requester, model, status, started_at) VALUES (?,?,?,?,?,?)",
                        (run_id, request, requester, model, "running", _now()))
        self.run_dir(run_id).mkdir(parents=True, exist_ok=True)
        return run_id

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def append(self, run_id: str, kind: str, /, **data) -> int:
        if run_id not in self._seq:
            row = self.db.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id = ?", (run_id,)).fetchone()
            self._seq[run_id] = row[0]
        self._seq[run_id] += 1
        seq = self._seq[run_id]
        self.db.execute("INSERT INTO events (run_id, seq, at, kind, data) VALUES (?,?,?,?,?)",
                        (run_id, seq, _now(), kind, _json(data)))
        for fn in self._listeners:
            fn(run_id, kind, data)
        return seq

    def llm_call(self, run_id: str, seq: int, request: dict, response: dict) -> None:
        self.db.execute("INSERT INTO llm_calls (run_id, seq, request, response) VALUES (?,?,?,?)",
                        (run_id, seq, _json(request), _json(response)))

    def update_run(self, run_id: str, **fields) -> None:
        for key in ("state", "snapshot"):
            if key in fields and fields[key] is not None and not isinstance(fields[key], str):
                fields[key] = _json(fields[key])
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), run_id))

    def end_run(self, run_id: str, status: str, summary: str = "") -> None:
        self.update_run(run_id, status=status, summary=summary, ended_at=_now())

    def beat(self, run_id: str) -> None:
        """The run is alive (a run marked running whose heartbeat stopped long ago was abandoned)."""
        self.update_run(run_id, heartbeat_at=_now())

    # ------------------------------------------------------------------ reading
    def run(self, run_id: str) -> dict | None:
        if run_id == "last":
            row = self.db.execute("SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()
        else:
            row = self.db.execute("SELECT * FROM runs WHERE id = ? OR id LIKE ?", (run_id, f"%{run_id}")).fetchone()
        return dict(row) if row else None

    def runs(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?",
                                                 (limit,))]

    def events(self, run_id: str) -> list[dict]:
        return [dict(r) | {"data": json.loads(r["data"])} for r in self.db.execute(
            "SELECT seq, at, kind, data FROM events WHERE run_id = ? ORDER BY seq", (run_id,))]

    def llm_calls(self, run_id: str) -> list[dict]:
        return [{"seq": r["seq"], "request": json.loads(r["request"]), "response": json.loads(r["response"])}
                for r in self.db.execute("SELECT * FROM llm_calls WHERE run_id = ? ORDER BY id", (run_id,))]
