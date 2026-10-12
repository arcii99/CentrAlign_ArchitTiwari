"""Paths, ports, the frozen world clock, and chaos profiles."""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPANY_SRC = Path(__file__).resolve().parent / "company"

# Overridable so tests can run an isolated world next to the dev one.
WORLD_DIR = Path(os.environ.get("NW_WORLD_DIR", ROOT / "var" / "world"))
PORTAL_DB = WORLD_DIR / "portal.db"
MAIL_DB = WORLD_DIR / "mail.db"
MAIL_FILES = WORLD_DIR / "mail_files"
DRIVE = WORLD_DIR / "drive"
STATE_FILE = WORLD_DIR / "state.json"
# Credentials the agent's runtime injects on its behalf; the model never sees them.
SECRETS_FILE = WORLD_DIR / "secrets.json"

PORTAL_PORT = int(os.environ.get("NW_PORTAL_PORT", 8101))
MAIL_PORT = int(os.environ.get("NW_MAIL_PORT", 8102))
PORTAL_URL = f"http://127.0.0.1:{PORTAL_PORT}"
MAIL_URL = f"http://127.0.0.1:{MAIL_PORT}"

# The world's clock is frozen so scenarios and checkers are reproducible.
DEFAULT_AS_OF = date(2026, 10, 5)

COMPANY_DOMAIN = "northwind.example"
AP_MAILBOX = f"ap@{COMPANY_DOMAIN}"

CHAOS_PROFILES: dict[str, dict] = {
    "off": {"enabled": False},
    "light": {
        "enabled": True,
        "modal_prob": 0.15,
        "session_expiry_prob": 0.03,
        "error_prob": 0.03,
        "post_commit_error_ratio": 0.5,
        "latency_ms": [100, 800],
        "shuffle_ids": False,
    },
    "heavy": {
        "enabled": True,
        "modal_prob": 0.3,
        "session_expiry_prob": 0.08,
        "error_prob": 0.08,
        "post_commit_error_ratio": 0.5,
        "latency_ms": [200, 2500],
        "shuffle_ids": True,
    },
}


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"scenario": None, "as_of": DEFAULT_AS_OF.isoformat(), "chaos": "off", "seed": 7}
    return json.loads(STATE_FILE.read_text())


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def today() -> date:
    return date.fromisoformat(load_state()["as_of"])


def now() -> datetime:
    """Business date from the frozen world clock, wall-clock time of day."""
    return datetime.combine(today(), datetime.now().time()).replace(microsecond=0)


def chaos_config() -> dict:
    state = load_state()
    return {"seed": state.get("seed", 7), **CHAOS_PROFILES[state.get("chaos", "off")]}
