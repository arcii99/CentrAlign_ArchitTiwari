"""How Nova reaches people: questions (Nova's own judgment) and approvals (required by policy).

Every request goes into a durable inbox (a table in runs.db), so it outlives the process. People answer when they
can, in the terminal right away, or later with `python -m agent approve|reject|answer <id>`. If nobody answers now,
the run pauses (`Pause`); `python -m agent resume <run>` continues it once they have.

    InboxHuman(prompt=True)   asks in the terminal at once; Ctrl+C or "later" at the prompt pauses the run instead
    InboxHuman(prompt=False)  never prompts: every request pauses the run until it is answered from the inbox
    ScriptedHuman             answers from rules, for tests and the benchmark (never pauses)
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Protocol

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

INBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, who TEXT, payload TEXT NOT NULL,
    status TEXT NOT NULL, answer TEXT, decided_by TEXT, created_at TEXT NOT NULL, decided_at TEXT
);
"""


class Pause(Exception):
    """A person hasn't answered yet. The run is saved and continues when they do."""

    def __init__(self, item_id: str, what: str):
        super().__init__(f"waiting for {what} ({item_id})")
        self.item_id, self.what = item_id, what


class Human(Protocol):
    def ask(self, question: str, requester: str, key: str, run_id: str) -> tuple[str, str]:
        """Returns (who answered, answer)."""

    def approve(self, approver: str, kind: str, details: dict, reason: str, key: str, run_id: str) -> tuple[bool, str, str]:
        """Returns (approved, who decided, comment)."""


class Inbox:
    """Requests to people, kept in runs.db so they outlive the process."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.executescript(INBOX_SCHEMA)
        if "remember" not in {r[1] for r in self.db.execute("PRAGMA table_info(inbox)")}:
            self.db.execute("ALTER TABLE inbox ADD COLUMN remember INTEGER NOT NULL DEFAULT 0")

    def get(self, item_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM inbox WHERE id = ?", (item_id,)).fetchone()
        return dict(row) | {"payload": json.loads(row["payload"])} if row else None

    def open(self, item_id: str, run_id: str, kind: str, who: str, payload: dict) -> dict:
        if not self.get(item_id):
            self.db.execute("INSERT INTO inbox (id, run_id, kind, who, payload, status, created_at) VALUES "
                            "(?,?,?,?,?,?,?)", (item_id, run_id, kind, who, json.dumps(payload, ensure_ascii=False),
                                                "open", datetime.now().isoformat(timespec="seconds")))
        return self.get(item_id)

    def decide(self, item_id: str, status: str, answer: str, by: str, remember: bool = False) -> dict:
        """`remember`: the person wants this answer (or rejection reason) kept as a standing instruction."""
        item = self.get(item_id)
        if not item:
            raise KeyError(f"no inbox item {item_id!r}")
        if item["status"] != "open":
            raise ValueError(f"{item_id} was already {item['status']} by {item['decided_by']}")
        self.db.execute("UPDATE inbox SET status = ?, answer = ?, decided_by = ?, decided_at = ?, remember = ? "
                        "WHERE id = ?", (status, answer, by, datetime.now().isoformat(timespec="seconds"),
                                         int(bool(remember)), item_id))
        return self.get(item_id)

    def pending(self, run_id: str | None = None) -> list[dict]:
        rows = self.db.execute("SELECT * FROM inbox WHERE status = 'open' AND (? IS NULL OR run_id = ?) "
                               "ORDER BY created_at", (run_id, run_id)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]


def _details(details: dict) -> str:
    return "\n".join(f"  {k}: {v}" for k, v in details.items())


class InboxHuman:
    def __init__(self, inbox: Inbox, prompt: bool = True, console: Console | None = None):
        self.inbox, self.prompt, self.console = inbox, prompt, console or Console()
        self.remembered: dict[str, bool] = {}  # inbox item → the person asked Nova to remember the answer

    def _remember(self) -> bool:
        try:
            return Prompt.ask("Remember this for next time, as a standing instruction?", choices=["y", "n"],
                              default="n") == "y"
        except (KeyboardInterrupt, EOFError):
            return False

    def _later(self, text: str) -> bool:
        return text.strip().lower() in ("later", "wait", "pause")

    def ask(self, question: str, requester: str, key: str, run_id: str) -> tuple[str, str]:
        item = self.inbox.open(key, run_id, "question", requester, {"question": question})
        if item["status"] == "open" and self.prompt:
            self.console.print(Panel(question, title=f"Nova has a question for {requester} ({key})", border_style="yellow"))
            try:
                answer = Prompt.ask(f"[yellow]Your answer[/] (as {requester}; 'later' to answer from the inbox)")
            except (KeyboardInterrupt, EOFError):
                answer = "later"
            if not self._later(answer):
                item = self.inbox.decide(key, "answered", answer, requester, remember=self._remember())
        if item["status"] == "open":
            raise Pause(key, f"an answer from {requester}")
        self.remembered[key] = bool(item.get("remember"))
        return item["decided_by"] or requester, item["answer"] or ""

    def approve(self, approver: str, kind: str, details: dict, reason: str, key: str, run_id: str) -> tuple[bool, str, str]:
        item = self.inbox.open(key, run_id, "approval", approver, {"kind": kind, "details": details, "reason": reason})
        if item["status"] == "open" and self.prompt:
            body = f"[b]{kind}[/b]\n{_details(details)}" + (f"\n\nWhy: {reason}" if reason else "")
            self.console.print(Panel(body, title=f"Approval requested from {approver} ({key})", border_style="magenta"))
            try:
                choice = Prompt.ask(f"[magenta]Approve as {approver}?[/] (y / n / later)", choices=["y", "n", "later"],
                                    default="y")
                comment = "" if choice == "later" else Prompt.ask("Comment (optional)", default="")
            except (KeyboardInterrupt, EOFError):
                choice = "later"
            if choice != "later":
                keep = choice == "n" and bool(comment) and self._remember()  # a rejection's reason can be a lesson
                item = self.inbox.decide(key, "approved" if choice == "y" else "rejected", comment, approver,
                                         remember=keep)
        if item["status"] == "open":
            raise Pause(key, f"a decision from {approver}")
        self.remembered[key] = bool(item.get("remember"))
        return item["status"] == "approved", item["decided_by"] or approver, item["answer"] or ""


class ScriptedHuman:
    """Rules file (JSON): {"answers": [{"match": "regex", "answer": "...", "remember": false}], "default_answer":
    "...", "reject": [{"match": "regex over the action and its details", "comment": "...", "remember": false}]}.
    A rejection judges what would be done, not the reason given: a reason may quote the wrong value to explain why
    it wasn't used. "remember": true marks an answer the person wants kept as a standing instruction."""

    def __init__(self, rules: dict | None = None):
        rules = rules or {}
        self.answers = rules.get("answers", [])
        self.default = rules.get("default_answer", "I have no more information. Follow the handbook.")
        self.reject = rules.get("reject", [])
        self.remembered: dict[str, bool] = {}

    @classmethod
    def from_file(cls, path: str | Path | None) -> "ScriptedHuman":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")) if path else None)

    def ask(self, question: str, requester: str, key: str = "", run_id: str = "") -> tuple[str, str]:
        for rule in self.answers:
            if re.search(rule["match"], question, re.I):
                self.remembered[key] = bool(rule.get("remember"))
                return requester, rule["answer"]
        return requester, self.default

    def approve(self, approver: str, kind: str, details: dict, reason: str, key: str = "",
                run_id: str = "") -> tuple[bool, str, str]:
        text = f"{kind} {json.dumps(details)}"
        for rule in self.reject:
            if re.search(rule["match"], text, re.I):
                self.remembered[key] = bool(rule.get("remember"))
                return False, approver, rule.get("comment", "Rejected.")
        return True, approver, ""
