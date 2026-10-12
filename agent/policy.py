"""The policy gate: Nova's permissions as code (rules in agent/policy.yaml).

Every consequential action is evaluated here before it happens: portal form submissions at the network layer (the
browser's request hook), emails before they are sent. The decision is allow, approval (an approval that covers
exactly this request is required) or deny. The model cannot talk its way past it, and choosing different clicks
doesn't help, because the check sits on the request itself.

Approvals are bound to their payload: an approval for 174000 on BILL-2026-0033 authorises that payment and nothing
else, once. If the request differs in a bound field, or the approval was already used, a new one is needed.
"""

from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

DEFAULT_FILE = Path(__file__).with_name("policy.yaml")
_ACCOUNT = re.compile(r"(?<![\w@./-])\d{9,18}(?![\w@-])")


@dataclass
class Decision:
    effect: str  # allow | approval | deny
    message: str = ""
    rules: list[str] = field(default_factory=list)
    approval_kind: str = ""
    approval_ref: str = ""  # the approval that authorises the request (consumed once it succeeds)

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"


def _canon(v: Any) -> Any:
    """Comparable form of a value: numbers as floats (commas, ₹ ignored), text trimmed and lower-cased."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (list, tuple, set)):
        return sorted(_canon(x) for x in v)
    s = re.sub(r"\s+", " ", str(v)).strip()  # an approved email re-typed with other line breaks is the same email
    m = re.fullmatch(r"(?:₹\s?|rs\.?\s?|inr\s?)?(-?\d[\d,]*(?:\.\d+)?)", s, re.I)
    if m:
        return float(m[1].replace(",", ""))
    return s.lower()


def _cites_earlier(record: Any, reason: Any) -> bool:
    """Does a reason ("Duplicate of BILL-2026-0004 – …") cite another record of the same kind that came before this
    one? Same kind = an identifier of the same shape; before = a smaller number (identifiers count up)."""
    me = str(record or "").strip().upper()
    if not me or not re.search(r"\d", me):
        return False
    shape = lambda s: re.sub(r"[A-Z]", "A", re.sub(r"\d", "9", s))  # noqa: E731
    number = lambda s: int("".join(re.findall(r"\d", s)))  # noqa: E731
    cited = [t.upper() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9/-]*\d[A-Za-z0-9/-]*", str(reason or ""))]
    return any(shape(c) == shape(me) and c != me and number(c) < number(me) for c in cited)


def _differs(have: str, approved: str) -> str:
    """Where a long text (an email body) departs from the approved one, briefly."""
    a, b = _canon(have), _canon(approved)
    i = next((n for n, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return f"…{str(a)[max(0, i - 30):i + 40]}… here, but …{str(b)[max(0, i - 30):i + 40]}… in the approval"


def _fill(text: str, env: dict) -> str:
    """A rule's message with {name} replaced by that request value or signal; other braces stay as they are."""
    return re.sub(r"\{(\w+)\}", lambda m: str(env[m[1]]) if m[1] in env else m[0], str(text)).strip()


_OPS = {ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
        ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
        ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}


def evaluate_condition(expr: str, env: dict) -> bool:
    """A rule condition such as `amount > 100000 and status == "posted"`: names, constants, comparisons, and/or/not.
    Anything else is rejected, so a policy file can't run code."""
    def ev(n: ast.AST):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.BoolOp):
            vals = [ev(v) for v in n.values]
            return all(vals) if isinstance(n.op, ast.And) else any(vals)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            return not ev(n.operand)
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, right in zip(n.ops, n.comparators):
                r = ev(right)
                try:
                    if not _OPS[type(op)](left, r):
                        return False
                except (TypeError, KeyError):
                    return False
                left = r
            return True
        if isinstance(n, ast.Name):
            return env.get(n.id)
        if isinstance(n, ast.Constant):
            return _canon(n.value) if isinstance(n.value, str) else n.value
        if isinstance(n, (ast.List, ast.Tuple)):
            return [ev(x) for x in n.elts]
        raise ValueError(f"unsupported policy condition: {expr!r}")
    return bool(ev(ast.parse(expr, mode="eval")))


class Policy:
    def __init__(self, spec: dict):
        self.company = spec.get("company") or {}
        self.approvals: dict[str, dict] = spec.get("approvals") or {}
        self.rules: list[dict] = spec.get("rules") or []
        self.default = spec.get("default", "allow")
        self.domain = str(self.company.get("internal_domain", "")).lower()
        self.own_accounts = {str(a) for a in self.company.get("own_bank_accounts") or []}
        for r in self.rules:
            if r.get("when"):
                ast.parse(r["when"], mode="eval")  # fail at load, not at the first payment
            if r.get("effect") == "approval" and r.get("approval") not in self.approvals:
                raise ValueError(f"rule {r.get('name')!r} needs an approval kind that is not defined")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Policy":
        p = Path(path or os.environ.get("NOVA_POLICY") or DEFAULT_FILE)
        return cls(yaml.safe_load(p.read_text(encoding="utf-8")))

    # ------------------------------------------------------------------ signals and fields
    def _signals(self, fields: dict, contacts: set[str]) -> dict:
        recipients = [str(a).strip().lower() for a in fields.get("recipients") or [] if str(a).strip()]
        text = f"{fields.get('subject', '')}\n{fields.get('body', '')}"
        numbers = set(_ACCOUNT.findall(text))
        external = [a for a in recipients if not a.endswith("@" + self.domain)]
        unregistered = [a for a in external if a not in contacts]
        return {"has_account_number": bool(numbers - self.own_accounts),
                "mentions_own_account": bool(numbers & self.own_accounts),
                "external": bool(external),
                "unregistered_external": bool(unregistered),
                "unregistered": ", ".join(unregistered),  # for messages: {unregistered}
                # a refund request is one by what it asks, not by whether it quotes our account (seen 2026-10-09: a
                # request without the account needed no approval, because the account was the only sign)
                "asks_for_refund": bool(re.search(r"\brefund", text, re.I)),
                "own_account": ", ".join(sorted(self.own_accounts)),  # for messages: {own_account}
                "cites_earlier_record": _cites_earlier(fields.get("bill_id"), fields.get("status_reason"))}

    # ------------------------------------------------------------------ approvals
    def _match(self, kind: str, spec: dict, fields: dict, approval) -> str:
        """'' if this approval covers the request, otherwise why not."""
        if approval.kind != kind or approval.status != "approved":
            return "not this kind"
        if approval.consumed_by:
            return f"approval {approval.ref} was already used for {approval.consumed_by}"
        diffs = []
        for req_key, detail_key in (spec.get("binds") or {}).items():
            have, approved = fields.get(req_key), approval.payload.get(detail_key)
            if isinstance(have, list):  # e.g. every recipient must be among the approved ones
                approved_list = approved if isinstance(approved, list) else re.split(r"[,;\s]+", str(approved or ""))
                ok = {_canon(x) for x in have} <= {_canon(x) for x in approved_list if str(x).strip()}
            else:
                ok = have not in (None, "") and _canon(have) == _canon(approved)
            if not ok and isinstance(have, str) and len(have) > 80:  # a body: say where, and how to proceed
                diffs.append(f"the {req_key} is not the text approved in {approval.ref} ({_differs(have, approved)}). "
                             f"Send exactly the approved text, or ask for a new approval of the new text")
            elif not ok:
                diffs.append(f"{req_key} is {have!r} here but {approved!r} in the approval")
        return "; ".join(diffs)

    def _approval_for(self, kind: str, fields: dict, approvals: list) -> tuple[str, str]:
        """(ref of an approval that covers the request, '') or ('', why none does)."""
        spec = self.approvals[kind]
        ref_field = spec.get("ref_field")
        cited = str(fields.get(ref_field) or "").strip() if ref_field else ""
        candidates = [a for a in approvals if a.kind == kind]
        if cited:
            mine = [a for a in candidates if a.ref.lower() == cited.lower()]
            if not mine:
                return "", f"{cited!r} is not an approval Nova was given for {kind.replace('_', ' ')} in this task"
            problem = self._match(kind, spec, fields, mine[0])
            return ("", problem) if problem else (mine[0].ref, "")
        reasons = []
        for a in candidates:
            problem = self._match(kind, spec, fields, a)
            if not problem:
                if ref_field:  # covered, but the record must cite it
                    return "", f"approval {a.ref} covers this: enter {a.ref} in the '{spec.get('ref_label', ref_field)}' field"
                return a.ref, ""
            reasons.append(problem)
        return "", "; ".join(r for r in reasons if r != "not this kind")

    # ------------------------------------------------------------------ the decision
    def evaluate(self, action: str, fields: dict, approvals: list, contacts: set[str] | None = None,
                 context: dict | None = None) -> Decision:
        """`context` holds facts about the run itself, such as `correcting` (the work was reported done)."""
        signals = {**self._signals(fields, {c.lower() for c in contacts or set()}), **(context or {})}
        env = {**{k: _canon(v) for k, v in fields.items() if not isinstance(v, dict)}, **signals}
        shown = {**fields, **signals}  # messages show the request's own values, not their comparable forms
        matched = [r for r in self.rules if any(fnmatch(action, pattern) for pattern in
                                                ([r["action"]] if isinstance(r["action"], str) else r["action"]))
                   and (not r.get("when") or evaluate_condition(r["when"], env))]
        names = [r["name"] for r in matched]
        denied = [r for r in matched if r.get("effect") == "deny"]
        if denied:
            return Decision("deny", " ".join(_fill(r["why"], shown) for r in denied) + " Nothing was sent.", names)
        refs = []
        for r in [r for r in matched if r.get("effect") == "approval"]:
            kind = r["approval"]
            spec = self.approvals[kind]
            ref, why_not = self._approval_for(kind, fields, approvals)
            if not ref:
                bound = {d: fields[k] for k, d in (spec.get("binds") or {}).items() if fields.get(k) not in (None, "")}
                need = ", ".join(f"{d}={json.dumps(bound[d], ensure_ascii=False)}" if d in bound else d
                                 for d in spec.get("details") or [])  # the values it must cover, filled in
                how = (f"request_approval(action=\"{kind}\", approver=\"{spec['approver']}\", details={{{need}}}) "
                       f"and wait for the decision" + (f", then enter the reference in the "
                                                       f"'{spec.get('ref_label', spec['ref_field'])}' field" if
                                                       spec.get("ref_field") else "") + ".")
                # a rule can name a way that needs no approval at all; it comes first, so nobody is asked needlessly
                instead = _fill(r.get("instead") or "", shown)
                how = f"{instead} Otherwise, call {how}" if instead else f"Call {how}"
                return Decision("approval", f"{_fill(r['why'], shown)} {why_not + '. ' if why_not else ''}{how} "
                                            "Nothing was sent.", names, approval_kind=kind)
            refs.append(ref)
        if self.default != "allow" and not matched:
            return Decision("deny", f"No rule allows {action}. Nothing was sent.", names)
        return Decision("allow", "", names, approval_ref=refs[0] if refs else "")

    def required_details(self, kind: str) -> list[str]:
        spec = self.approvals.get(kind) or {}
        return list(dict.fromkeys(list((spec.get("binds") or {}).values())))
