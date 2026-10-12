"""The evidence report of a run (report.md for people, evidence.json for tools).

It is assembled from the event log, the contract and the verifier's fresh reads, never from Nova's own account of
what it did: what was asked, what Nova promised before acting, what it did, which guards stopped it, who approved
what, and what an independent read of the systems confirmed.
"""

from __future__ import annotations

from agent.state import RunState

MARK = {"pass": "✓ confirmed", "fail": "✗ failed", "unverified": "– not checkable by reading the systems"}
HEADLINES = {
    "verified": "Done, and confirmed by an independent read of the systems.",
    "finished": "Done. Nothing could be confirmed by reading the systems.",
    "verification_failed": "Finished, but the independent check found problems (see Verification).",
    "waiting": "Paused: waiting for a person (see Questions and approvals). Resume once they have answered.",
    "blocked": "Stopped: a system kept failing, so Nova stopped rather than guess.",
    "stuck": "Stopped: Nova kept repeating itself.",
    "out_of_steps": "Stopped: the step budget ran out.",
    "out_of_time": "Stopped: the time budget ran out.",
    "not_started": "Not started: a system Nova needs was not reachable.",
    "incomplete": "Not every item was done (see Work items).",
}


def _cell(s) -> str:
    return str(s).replace("|", "/").replace("\n", " ")


def build(st: RunState, status: str, events: list[dict], verification: dict | None, model: str) -> tuple[str, dict]:
    calls = sum(1 for e in events if e["kind"] == "llm.reply")
    started, ended = (events[0]["at"], events[-1]["at"]) if events else ("", "")
    policy = [e["data"] for e in events if e["kind"] == "policy" and e["data"]["effect"] != "allow"]
    allowed_with = [e["data"] for e in events if e["kind"] == "policy" and e["data"]["effect"] == "allow"
                    and e["data"].get("approval")]
    guards = [(e["kind"], e["data"]) for e in events if e["kind"] in ("provenance.blocked", "review", "review.override",
                                                                      "runtime.refused")]
    guards = [(k, d) for k, d in guards if k != "review" or d.get("problems")]

    lines = [f"# Nova run {st.run_id}", "", f"**{HEADLINES.get(status, status)}**", "", "| | |", "|---|---|",
             f"| Request | \"{_cell(st.request)}\" ({_cell(st.requester)}) |", f"| Business date | {st.today} |",
             f"| Outcome | `{status}` |", f"| Model | {model}: {calls} model calls, {started} → {ended} |", ""]

    if st.work_items:  # a split request: each item has its own run and report
        lines += ["## Work items", "", "The request covered several documents, so each was done as its own task, one "
                  "after another. Each item's report has its contract, verification, approvals and policy decisions.",
                  "", "| # | Item | Outcome | Report |", "|---|---|---|---|"]
        lines += [f"| {n} | {_cell(i['request'])} | `{i['status']}` | "
                  + (f"[{i['run_id']}](../{i['run_id']}/report.md)" if i.get("run_id") else "not started") + " |"
                  for n, i in enumerate(st.work_items, 1)]
        return "\n".join(lines), {"run": st.run_id, "request": st.request, "requester": st.requester,
                                  "status": status, "model": model, "model_calls": calls,
                                  "work_items": st.work_items}

    lines += ["## Contract (written before acting)", "", f"**Goal:** {st.goal or '(no contract was written)'}", ""]
    if st.facts:
        lines += ["**Facts from the documents** (each one found in the documents by code):", ""]
        lines += [f"- {f['name']}: {f['value']}" + (f" ({f['source']})" if f.get("source") else "") for f in st.facts]
        lines.append("")
    if st.success_criteria:
        lines += ["**Done when:**", ""] + [f"{i}. {c}" for i, c in enumerate(st.success_criteria, 1)] + [""]

    lines += ["## Verification (independent, read-only)", ""]
    if not verification:
        lines += ["Not run: the task did not reach finish.", ""]
    else:
        n = verification["counts"]
        lines += [f"Verdict: **{verification['verdict']}** ({n['pass']} confirmed, {n['fail']} failed, "
                  f"{n['unverified']} not checkable by reading the systems).", ""]
        if verification["criteria"]:
            lines += ["| # | Success criterion | Checked by | Result |", "|---|---|---|---|"]
            for i, c in enumerate(verification["criteria"], 1):
                p = c["probe"]
                how = {"portal_page": f"portal page {p.get('path', '')}", "mail_sent": f"Sent mail to {p.get('to', '?')}",
                       "sheet_row": f"row in {p.get('path', '')}"}.get(p.get("kind"), "—")
                lines.append(f"| {i} | {_cell(c['criterion'])} | {_cell(how)} | {MARK[c['status']]}: "
                             f"{_cell(c.get('detail', ''))} |")
            lines.append("")
        if verification["actions"]:
            lines += ["**Every action, read back:**", "", "| Action | Result |", "|---|---|"]
            lines += [f"| {_cell(a['what'])} | {MARK[a['status']]}: {_cell(a['detail'])} |"
                      for a in verification["actions"]]
            lines.append("")

    used = [e["data"] for e in events if e["kind"] == "lesson.used"]
    learned = [e["data"] for e in events if e["kind"] == "lesson.learned"]
    if st.lessons or used or learned:
        lines += ["## Memory (standing instructions from people)", ""]
        lines += [f"- Recalled for this task: {x}" for x in st.lessons]
        lines += [f"- **Answered from memory, nobody interrupted:** \"{_cell(u['question'][:200])}\" → {u['id']} "
                  f"({_cell(u['by'])}): \"{_cell(u['guidance'][:200])}\"" for u in used]
        lines += [f"- **Learned** {x['id']} from {_cell(x['by'])} ({x['kind']}): \"{_cell(x['guidance'][:200])}\""
                  for x in learned]
        lines.append("")

    lines += ["## Approvals", ""]
    decided = {a.ref for a in st.approvals}  # an approval joins the state once decided; until then it is only asked
    asked = [e["data"] for e in events if e["kind"] == "approval.request" and e["data"]["ref"] not in decided]
    if st.approvals or asked:
        lines += ["| Ref | Action | Details | Approver | Decision | Used by |", "|---|---|---|---|---|---|"]
        lines += [f"| {a.ref} | {a.kind} | {_cell(', '.join(f'{k}={v}' for k, v in a.payload.items()))} | "
                  f"{_cell(a.approver)} | {a.status} by {_cell(a.by)}{': ' + _cell(a.comment) if a.comment else ''} | "
                  f"{_cell(a.consumed_by or '—')} |" for a in st.approvals]
        lines += [f"| {d['ref']} | {d['kind']} | {_cell(', '.join(f'{k}={v}' for k, v in d['payload'].items()))} | "
                  f"{_cell(d['approver'])} | **waiting for a decision** | — |" for d in asked]
    else:
        lines.append("None needed.")
    lines.append("")

    lines += ["## Policy decisions", ""]
    if policy or allowed_with:
        lines += [f"- **{d['effect']}** {d['action']}: {_cell(d['message'])}" for d in policy]
        lines += [f"- **allowed** {d['action']} with approval {d['approval']}" for d in allowed_with]
    else:
        lines.append("Every consequential action was within policy on the first try.")
    lines.append("")

    lines += ["## Guards that stopped something", ""]
    if guards:
        for k, d in guards:
            if k == "provenance.blocked":
                lines.append(f"- provenance: {d['name']} carried values seen nowhere in the task: {'; '.join(d['values'])}")
            elif k == "review":
                lines += [f"- maker-checker: {p['field']} = {p['value']}, worked out again: {p['checker']}"
                          for p in d["problems"]]
            elif k == "review.override":
                lines.append(f"- maker-checker overridden (resubmitted unchanged): {d.get('fields') or d.get('values')}")
            else:
                lines.append(f"- repeat refused: {d.get('name')} ({d.get('times')} times before)")
    else:
        lines.append("None.")
    lines.append("")

    lines += ["## Plan and what was done", ""]
    for i, s in enumerate(st.plan, 1):
        mark = {"done": "[done]", "skipped": "[skipped]"}.get(s.status, "[open]")
        lines.append(f"{i}. {mark} {s.text}" + (f" → {_cell(s.result)}" if s.result else ""))
    lines += ["" if st.plan else "(no plan)", "## Questions to people", ""]
    lines += [f"- {_cell(q['question'])} → {_cell(q['answer'])} ({_cell(q.get('by', ''))})" for q in st.questions] \
        or ["None."]
    lines += ["", "## Nova's summary", "", st.summary or "(none)", ""]

    evidence = {"run": st.run_id, "request": st.request, "requester": st.requester, "business_date": st.today,
                "status": status, "model": model, "model_calls": calls, "goal": st.goal, "facts": st.facts,
                "success_criteria": st.success_criteria, "verification": verification,
                "approvals": [a.__dict__ for a in st.approvals], "policy": policy,
                "guards": [{"kind": k, **d} for k, d in guards],
                "plan": [s.__dict__ for s in st.plan], "questions": st.questions, "summary": st.summary}
    return "\n".join(lines), evidence
