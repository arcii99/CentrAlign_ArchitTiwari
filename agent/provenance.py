"""Where did that value come from?

Before a consequential action (submitting a form, adding a tracker row, sending an email, asking for an approval)
every identifier, date and amount it carries must already have been seen in this task: in a document, a page,
a person's answer, or a calc/date_calc result. A small model that types a due date "from memory" is stopped and
told to compute it or copy it from where it appears. The handbook says "never invent values"; this makes the
runtime enforce it instead of trusting the model to remember.
"""

from __future__ import annotations

import difflib
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_DATE_RES = [
    (re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b"), "dmy"),
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
    (re.compile(rf"\b(\d{{1,2}})[- ]({_MONTH})[-, ]+(\d{{4}})\b", re.I), "dMy"),
    (re.compile(rf"\b({_MONTH}) (\d{{1,2}}),? (\d{{4}})\b", re.I), "Mdy"),
]
_ID = re.compile(r"(?<![\w/.-])[A-Za-z0-9][A-Za-z0-9/_.-]*\d[A-Za-z0-9/_.-]*")
_NUM = re.compile(r"(?<![\w.])(?:₹\s?|rs\.?\s?|inr\s?)?(\d[\d,]*(?:\.\d+)?)", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _month(name: str) -> int:
    return datetime.strptime(name[:3].title(), "%b").month


def _dates(text: str) -> list[tuple[str, str]]:
    """(matched text, ISO date) for every date written in a common format."""
    out = []
    for rx, kind in _DATE_RES:
        for m in rx.finditer(text):
            try:
                if kind == "dmy":
                    d = date(int(m[3]), int(m[2]), int(m[1]))
                elif kind == "ymd":
                    d = date(int(m[1]), int(m[2]), int(m[3]))
                elif kind == "dMy":
                    d = date(int(m[3]), _month(m[2]), int(m[1]))
                else:
                    d = date(int(m[3]), _month(m[1]), int(m[2]))
            except ValueError:
                continue
            out.append((m[0], d.isoformat()))
    return out


def _canon(raw: str) -> str | None:
    try:
        d = Decimal(raw.replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    s = format(d, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def as_date(value: Any) -> str | None:
    """ISO date if the whole value is a date in a common format."""
    s = str(value).strip()
    found = _dates(s)
    return found[0][1] if found and found[0][0] == s else None


def as_number(value: Any) -> str | None:
    """Canonical number if the whole value is an amount ('₹1,74,000.00' → '174000')."""
    if isinstance(value, bool):
        return None
    m = re.fullmatch(r"(?:₹\s?|rs\.?\s?|inr\s?)?(\d[\d,]*(?:\.\d+)?)", str(value).strip(), re.I)
    return _canon(m[1]) if m else None


def _likeness(a: str, b: str) -> float:
    """How alike two addresses are: by the name before the @ when the domain is the same (else a shared company
    domain alone makes priya.nair@ look like ananya.iyer@)."""
    (la, _, da), (lb, _, db) = a.partition("@"), b.partition("@")
    return difflib.SequenceMatcher(None, la, lb).ratio() if da == db else difflib.SequenceMatcher(None, a, b).ratio()


class Evidence:
    """Everything observed in this task, indexed by identifier, date and number."""

    def __init__(self) -> None:
        self.ids: set[str] = set()
        self.dates: set[str] = set()
        self.numbers: set[str] = set()
        self.emails: set[str] = set()

    def dump(self) -> dict:
        return {"ids": sorted(self.ids), "dates": sorted(self.dates), "numbers": sorted(self.numbers),
                "emails": sorted(self.emails)}

    @classmethod
    def load(cls, d: dict) -> "Evidence":
        ev = cls()
        ev.ids, ev.dates, ev.numbers = set(d.get("ids", [])), set(d.get("dates", [])), set(d.get("numbers", []))
        ev.emails = set(d.get("emails", []))
        return ev

    def add(self, text: str) -> None:
        text = str(text or "")
        self.ids.update(t.rstrip(".,;:").upper() for t in _ID.findall(text))
        self.dates.update(iso for _, iso in _dates(text))
        self.numbers.update(c for c in (_canon(n) for n in _NUM.findall(text)) if c)
        self.emails.update(e.rstrip(".").lower() for e in EMAIL.findall(text))

    def closest(self, address: str) -> str:
        """The address seen in this task that this one is most likely a misspelling of ('' if none is close)."""
        a = str(address).strip().lower()
        near = max(self.emails, key=lambda e: _likeness(a, e), default="")
        return near if near and _likeness(a, near) >= 0.75 else ""

    def unknown_addresses(self, addresses: Any) -> list[str]:
        """Recipients nothing in this task mentions, each with the closest address that was seen (seen 2026-10-09:
        the vendor ID went to "anya.iyer@…" while the requester was ananya.iyer@…; the domain was ours, so no rule
        stopped it)."""
        items = addresses if isinstance(addresses, list) else re.split(r"[,;\s]+", str(addresses or ""))
        out = []
        for a in (str(x).strip().lower() for x in items):
            if not a or a in self.emails:
                continue
            near = self.closest(a)
            out.append(f"{a} (did you mean {near}?)" if near else a)
        return out

    def missing(self, value: Any) -> list[str]:
        """The parts of a value that nothing in this task supports (empty = all good)."""
        if isinstance(value, bool) or value is None:
            return []
        if isinstance(value, (int, float)):
            c = _canon(str(value))
            return [] if c in self.numbers else [str(value)]
        if isinstance(value, dict):
            return [m for v in value.values() for m in self.missing(v)]
        if isinstance(value, list):
            return [m for v in value for m in self.missing(v)]
        text, out = str(value).strip(), []
        for e in EMAIL.findall(text):  # an address counts only exactly as seen
            if e.rstrip(".").lower() not in self.emails:
                out.append(e)
            text = text.replace(e, " ")
        for raw, iso in _dates(text):
            if iso not in self.dates:
                out.append(raw)
            text = text.replace(raw, " ")
        whole = re.fullmatch(r"(?:₹\s?|rs\.?\s?|inr\s?)?(\d[\d,]*(?:\.\d+)?)", text.strip(), re.I)
        if whole:  # the value is just a number: every number counts, even small ones
            return out + ([] if _canon(whole[1]) in self.numbers else [text.strip()])
        for token in _ID.findall(text):
            t = token.rstrip(".,;:")
            if re.fullmatch(r"[\d,.]+", t):
                continue  # plain numbers are handled below
            if t.upper() not in self.ids:
                out.append(t)
            text = text.replace(token, " ")
        for n in _NUM.findall(text):
            c = _canon(n)
            if c and len(c.split(".")[0]) >= 4 and c not in self.numbers:  # amounts; small counts are free text
                out.append(n)
        return out


# Which arguments of which tools carry values into the company's systems.
CHECKED = {"browser_fill": ("fields",), "sheet_append_row": ("row",), "sheet_write": ("rows",),
           "mail_send": ("subject", "body"), "mail_forward": ("body",), "request_approval": ("details",)}


def unsupported(tool: str, args: dict, evidence: Evidence) -> list[str]:
    if tool not in CHECKED or not isinstance(args, dict):
        return []
    out: list[str] = []
    if tool in ("mail_send", "mail_forward"):  # an address typed from memory is a misdirected email
        out += [f"recipient {a}" for key in ("to", "cc") for a in evidence.unknown_addresses(args.get(key))]
    for key in CHECKED[tool]:
        value = args.get(key)
        if tool == "browser_fill" and isinstance(value, dict):  # report which field each value went into
            out += [f"{label} = {bad}" for label, v in value.items() for bad in evidence.missing(v)]
        elif tool == "sheet_append_row" and isinstance(value, dict):
            out += [f"{col} = {bad}" for col, v in value.items() for bad in evidence.missing(v)]
        else:
            out += evidence.missing(value)
    return list(dict.fromkeys(out))
