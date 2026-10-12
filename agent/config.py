"""Nova's settings: where the company's resources are, what counts as a consequential action, and Nova's limits.

Everything has a default that matches the local mock company, so `python -m agent run "..."` works out of the box.
Override with environment variables (or a .env file):

    NOVA_DRIVE      shared drive root                                  (var/world/drive)
    NOVA_VAULT      endpoints + credentials per system; only the runtime reads it, never the model
                                                                       (var/world/secrets.json)
    NOVA_RUNS_DIR   event log and per-run files                        (var/runs)
    NOVA_TODAY      business date, DD-MM-YYYY (default: the date the Finance Portal shows)
    NOVA_LLM_*      the model, see agent/llm.py
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _path(var: str, default: Path) -> Path:
    return Path(os.environ[var]) if os.environ.get(var) else default


@dataclass
class Settings:
    drive: Path
    vault: Path
    runs_dir: Path
    today: str | None = None  # DD-MM-YYYY; None = ask the systems of record
    max_steps: int = 80  # model calls per run
    max_minutes: float = 45
    keep_full: int = 3  # tool results shown in full; older ones shrink to their one-line summary
    think: str = "recovery"  # never | recovery (after repeated failures) | always (incl. the contract)
    headless: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(drive=_path("NOVA_DRIVE", ROOT / "var" / "world" / "drive"),
                   vault=_path("NOVA_VAULT", ROOT / "var" / "world" / "secrets.json"),
                   runs_dir=_path("NOVA_RUNS_DIR", ROOT / "var" / "runs"),
                   today=os.environ.get("NOVA_TODAY") or None)

    def load_vault(self) -> dict:
        if not self.vault.exists():
            raise FileNotFoundError(f"no credentials vault at {self.vault} (reset the world first: "
                                    f"uv run python -m world reset t1_pay_globex)")
        return json.loads(self.vault.read_text())


# --------------------------------------------------------------------------- app manifests
# Consequential requests per system, named the way the audit trail and the policy (agent/policy.yaml) refer to
# them; named groups become request fields (the vendor a bank account is added to). The browser hands every POST
# to the policy gate; anything not listed here is navigation (search forms, sign-in, notices).

ACTIONS: dict[str, list[tuple[str, str, str]]] = {
    "portal": [
        ("POST", r"/bills/new", "portal.bill.create"),
        ("POST", r"/bills/(?P<bill_id>[^/]+)/status", "portal.bill.status"),
        ("POST", r"/payments/new", "portal.payment.create"),
        ("POST", r"/vendors/new", "portal.vendor.create"),
        ("POST", r"/vendors/(?P<vendor_id>[^/]+)/bank/new", "portal.bank_account.create"),
    ],
}

# Where a system shows its business date (the mock company's clock is frozen; a real one would match the wall
# clock, but reading it from the system of record is right either way).
CLOCKS: dict[str, str] = {"portal": r"Business date (\d{2}-\d{2}-\d{4})"}

# Where a system shows a vendor's registered contact email (the policy lets Nova write to these without approval).
CONTACTS: dict[str, str] = {"portal": r"Contact email: ([^\s|]+@[^\s|]+)"}
# Did a consequential request go through, when the system answered with an error? (Under chaos, half of the portal's
# 503s come AFTER the change was saved, and the portal accepts duplicates: a blind retry pays twice.) A page to read
# and a pattern on it that shows the record; {field} is a value from the request, {field_last4} its last 4 characters.
# The fields are also the request's identity: once it has gone through, the same request is not sent again.
CONFIRM: dict[str, tuple[str, str]] = {
    "portal.bill.create": ("/bills?q={vendor_invoice_no}", "{vendor_invoice_no}"),
    "portal.payment.create": ("/bills/{bill_id}", r"PAY-\d{4}-\d{4}"),
    "portal.bill.status": ("/bills/{bill_id}", "{status}"),
    "portal.vendor.create": ("/vendors?q={gstin}", "{gstin}"),
    "portal.bank_account.create": ("/vendors/{vendor_id}", "XXXX{account_number_last4}"),
}

# Records that exist once: before one is created, the gate looks for it in the system with its CONFIRM recipe, and
# refuses to create it again (seen 2026-10-09: a bill entered for an invoice already paid; a second bank account
# after a 503 that came after the save). Payments are not here: the portal itself refuses to pay a paid bill.
UNIQUE = ("portal.bill.create", "portal.vendor.create", "portal.bank_account.create")

# What a vendor ID looks like in a system, and the page that shows that vendor's record. Before the gate holds an
# email to an outside address it doesn't know, it reads the records of the vendors this task has dealt with.
VENDOR_PAGES: dict[str, tuple[str, str]] = {"portal": (r"\bV-\d{4}\b", "/vendors/{id}")}


def action_for(site: str, method: str, path: str) -> tuple[str | None, dict]:
    """(action name, fields taken from the path) for a request, or (None, {}) if it isn't consequential."""
    for m, pattern, name in ACTIONS.get(site, []):
        hit = re.fullmatch(pattern, path.split("?")[0])
        if m == method.upper() and hit:
            return name, hit.groupdict()
    return None, {}
