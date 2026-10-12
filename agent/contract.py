"""The task contract: what "done" means, written before acting.

At the end of UNDERSTAND the model gets one focused call (schema-constrained JSON, thinking on) that sees the
request, the chosen handbook procedures and the full text of every document it read, and writes:

    goal              the outcome, with its key identifiers
    facts             the values the work depends on, copied from the documents, each with its source
    success_criteria  the checks that will prove the work is done
    steps             every step of the procedure that applies, in order
    questions         what must be asked before starting

The runtime then checks each fact against the documents (grounding) and shows the facts back to the model on
every turn, so a value read once is never re-typed from memory. Phase 2's verifier turns the success criteria
into read-only probes.
"""

from __future__ import annotations

import re
from datetime import date

from agent.provenance import EMAIL
from agent.tools import ToolError, parse_date

SCHEMA = {
    "type": "object",
    "properties": {
        "work_items": {"type": "array", "items": {"type": "string"}},  # first: one document, or several?
        "goal": {"type": "string"},
        "facts": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "value": {"type": "string"}, "source": {"type": "string"}},
            "required": ["name", "value", "source"]}},
        "success_criteria": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["work_items", "goal", "facts", "success_criteria", "steps", "questions"],
}

SYSTEM = ("You are Nova, the AI accounts-payable associate at Northwind Robotics. You write the task contract for a "
          "request before any work starts: exact facts from the documents, the checks that prove the work is "
          "done, and the procedure's steps. You only write what the documents and the handbook support. "
          "Anything kept in the company's systems (the Finance Portal holds vendors, their terms and bank accounts, "
          "purchase orders, receipts, bills and payments; the AP mailbox; the shared drive) is looked up while "
          "carrying out the plan, so it belongs in a step, not in a question.")

PROMPT = """Request from {requester}: "{request}"
Today is {today}.

{handbook}

=== Every document you read (one line each) ===
{index}

=== Documents you read for this request (full text, as much as fits) ===
{documents}

{lessons}=== Your draft ===
{draft}

Write the task contract as JSON:
- work_items: {work_items}
- goal: the outcome in one sentence, with the key identifiers (for example the invoice number and the vendor).
- facts: the values printed in the documents above that the work will need (identifiers, dates, amounts, names, emails), each copied exactly as printed and named after its label in the document. One fact per value, with the document it came from. A document can show several parties (for example the vendor and us, the buyer), so say whose value it is. Only printed values: anything the procedure computes (tax, net amounts, due dates, payment dates) is worked out with calc and date_calc while carrying out the plan, so it is not a fact.
- success_criteria: what someone checking the company's systems afterwards will find if the work was done: each record, email or spreadsheet row the procedure produces, named by identifiers printed in the documents (invoice number, PO, vendor, email address). No values the procedure works out later (tax, net amounts, due and payment dates, the payment method): the independent check compares those with what was submitted. Results only, not the checks made along the way (those are steps). Where the procedure decides the outcome along the way (paid, or put on hold when a check fails; a duplicate or not), don't assume one: write it the way the procedure decides it ("the bill for the invoice is either paid, or on hold with the handbook's reason"). For an email or document the procedure produces, list everything the procedure's step says it must contain (the references it says to quote, whose details it must give), not just that it was sent.
- steps: the handbook procedure's own steps that apply to this request, in the handbook's order and with the handbook's step titles (a step titled "Step 4 — Do something" becomes "Do something"). Add a step only for work the handbook requires that has no step of its own. No values, dates or amounts in the steps: the handbook's text for each step is shown when you carry it out, and values come from the facts and from calculations made then.
- questions: only what you cannot find in these documents or in the company's systems, or where the documents contradict each other. Usually an empty list."""


WORK_ITEMS = ("look at every document listed above. If the request covers several separate documents (for example "
              "several invoice emails in the inbox), "
              "one self-contained request per document, such as \"Process the invoice in email #12 (Vandelay, "
              "VAN-88231)\": what to handle and where it is, not what the outcome should be. Each item then gets its "
              "own contract, so leave facts, success_criteria and steps empty. For a request about one document: an "
              "empty list.")
ONE_ITEM = "always an empty list (this request is already one item of a larger one)."


def documents_from(messages: list[dict], limit_each: int = 3500, limit_total: int = 9000) -> str:
    """The full text of the case documents the model read during UNDERSTAND: emails, attachments and documents on
    the drive. Spreadsheets are records (logs, reports), not the case itself, so they are left out."""
    parts, total = [], 0
    for m in messages:
        if m.get("role") != "tool" or m.get("tool_name") not in ("mail_read", "mail_attachment", "file_read") \
                or not m.get("ok", True) or "(spreadsheet)" in m["content"].split("\n", 1)[0]:
            continue
        text = m["content"][:limit_each]
        if total + len(text) > limit_total:
            break
        parts.append(text)
        total += len(text)
    return "\n\n---\n\n".join(parts) or "(none)"


def index_from(messages: list[dict]) -> str:
    """One line for every case document read, including those whose full text didn't fit: whether a request covers
    one document or several is decided from all of them (seen 2026-10-08: four invoices read, the text of one shown,
    one work item written)."""
    lines = [f"- {m['summary']}" for m in messages if m.get("role") == "tool" and m.get("ok", True)
             and m.get("tool_name") in ("mail_read", "mail_attachment", "file_read")
             and "(spreadsheet)" not in m["content"].split("\n", 1)[0]]
    return "\n".join(dict.fromkeys(lines)) or "(none)"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def _date_forms(value: str, today: date) -> list[str]:
    try:
        d = parse_date(value, today)
    except ToolError:
        return []
    return [f"{d:%d-%m-%Y}", f"{d:%d/%m/%Y}", f"{d:%d.%m.%Y}", f"{d:%Y-%m-%d}", f"{d:%d-%b-%Y}", f"{d:%d %b %Y}",
            f"{d:%d %B %Y}", f"{d:%B %d, %Y}"]


def grounded(value: str, sources: str, today: date) -> bool:
    """Is this value actually written in the sources? Lenient about number grouping and date formats; a number
    must appear as a whole number (5678 is not 'in' the GSTIN 29AAFCN5678J1Z4)."""
    v = _norm(value)
    if not v:
        return False
    # an address is all or nothing (seen 2026-10-09: "anya.iyer@…" passed as a substring of "ananya.iyer@…")
    found = {e.lower() for e in EMAIL.findall(sources)}
    if any(e.lower() not in found for e in EMAIL.findall(value)):
        return False
    num = re.fullmatch(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d+)?)", v)
    if num:
        want = float(num[1].replace(",", ""))
        return any(float(m[0].replace(",", "")) == want
                   for m in re.finditer(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])", sources))
    text = _norm(sources)
    return v in text or any(_norm(f) in text for f in _date_forms(value, today))


def clean_steps(steps: list) -> list[str]:
    """Strip the model's own numbering ('3.', 'Step 3 —', '6. a:'); the runtime numbers the plan."""
    out = []
    for s in steps:
        t = re.sub(r"^\s*(step\s*)?\d+\s*[a-z]?\s*[.):—–-]*\s*", "", str(s), flags=re.I).strip()
        t = re.sub(r"^[a-z][.):]\s+", "", t).strip()
        if t:
            out.append(t)
    return out
