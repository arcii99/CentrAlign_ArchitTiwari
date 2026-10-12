"""Maker-checker by independent re-derivation, before a consequential commit.

Finance teams don't let the person who enters a payment be the only one who checks it. Here the checker is the
same small model in a short, separate session with one job: work out each date and amount the form needs, from
the handbook section that governs the form, the facts and what has been seen, using calc and date_calc (never
mental arithmetic). If its values disagree with what the maker is about to submit, the commit is stopped and the
maker is shown the derivation, which is tool output it can check, not an opinion.

Measured on T1 before this design: a checker asked only to *judge* the values (no tools) flagged the wrong due
date 3/3 but proposed wrong corrections and objected to correct dates. Re-deriving with tools is what the model
does reliably (the model probe: due dates right 3/3 with a date tool).

The checker can only stop an action, never change it. If the maker resubmits exactly the same values, the runtime
lets them through and logs an override, so a wrong checker cannot block the work forever.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from agent.llm import LLMError
from agent.provenance import Evidence, as_date, as_number

SYSTEM = ("You are the checker in a maker-checker control in the accounts-payable team at Northwind Robotics. You "
          "work out values yourself, exactly as the handbook says, using the calc and date_calc tools for every "
          "calculation (never in your head). Then you call answer.")

PROMPT = """Another associate is about to {action}. Work out these values yourself: {fields}.
Today is {today}.

=== Handbook rules for this ===
{rules}

=== Known facts (from the documents, and the associate's notes) ===
{facts}

=== What has been seen recently ===
{seen}

=== The page on screen ===
{page}

Follow the handbook rules above for each value; when a rule says how a value is worked out, use the inputs it names and calc / date_calc to compute it. Then call answer with every value."""

TOOLS = [
    {"type": "function", "function": {"name": "calc", "description": "Exact arithmetic, e.g. '150000 * 0.02'.",
                                      "parameters": {"type": "object", "properties": {"expression": {"type": "string"}},
                                                     "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "date_calc", "description": "Convert a date to DD-MM-YYYY, optionally add days, compare with today.",
        "parameters": {"type": "object", "properties": {"date": {"type": "string"}, "add_days": {"type": "integer"}},
                       "required": ["date"]}}},
    {"type": "function", "function": {
        "name": "answer", "description": "Give the values you worked out: {field label: value}.",
        "parameters": {"type": "object", "properties": {"values": {"type": "object"}}, "required": ["values"]}}},
]


def reviewable(values: dict) -> dict:
    """The values a rule derives: dates and amounts. Identifiers, choices and references are covered by the
    provenance check and the system's own validation."""
    return {k: v for k, v in values.items() if as_date(v) or as_number(v)}


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def same(a, b) -> bool:
    return (as_date(a) or as_number(a) or _key(a)) == (as_date(b) or as_number(b) or _key(b))


def rederive(llm, tool_fns: dict[str, Callable[[dict], str]], action: str, fields: list[str], today: str, rules: str,
             facts: str, seen: str, page: str, max_calls: int = 8) -> tuple[dict, list[str]]:
    """Run the checker's short session. Returns (values it worked out, the tool trace)."""
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": PROMPT.format(action=action, fields=", ".join(fields), today=today,
                                                          rules=rules, facts=facts, seen=seen, page=page)}]
    trace: list[str] = []
    for n in range(max_calls):
        last = n == max_calls - 1
        if last:  # out of time for working: answer with what has been worked out
            messages.append({"role": "user", "content": "Call answer now with the values you have worked out."})
        try:
            reply = llm.chat(messages, tools=TOOLS[-1:] if last else TOOLS, think=False, max_tokens=600)
        except LLMError:
            return {}, trace
        if not reply.tool_calls:
            messages += [{"role": "assistant", "content": reply.content or ""},
                         {"role": "user", "content": "Call calc / date_calc for the calculations, then answer."}]
            continue
        messages.append(reply.as_message())
        for call in reply.tool_calls:
            if call.name == "answer":
                values = call.arguments.get("values") if isinstance(call.arguments, dict) else None
                if isinstance(values, str):
                    try:
                        values = json.loads(values)
                    except ValueError:
                        values = None
                return (values if isinstance(values, dict) else {}), trace
            fn = tool_fns.get(call.name)
            out = fn(call.arguments if isinstance(call.arguments, dict) else {}) if fn else f"no tool {call.name}"
            trace.append(out)
            messages.append({"role": "tool", "tool_name": call.name, "content": out})
    return {}, trace


def disagreements(maker: dict, checker: dict, trace: list[str]) -> list[dict]:
    """Fields whose maker value differs from the checker's re-derived value. The checker is held to the maker's
    standard: its value counts only if one of its own calc / date_calc results produced it (an answer it made up
    without working it out is ignored)."""
    worked = Evidence()
    for t in trace:
        worked.add(t)
    by_key = {_key(k): v for k, v in checker.items()}
    out = []
    for label, value in maker.items():
        mine = by_key.get(_key(label))
        if mine is not None and (as_date(mine) or as_number(mine)) and not same(value, mine) \
                and not worked.missing(mine):
            out.append({"field": label, "value": value, "checker": mine})
    return out
