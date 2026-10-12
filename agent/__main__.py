"""Nova's command line.

    uv run python -m agent run "Pay the Globex invoice" [--human terminal|inbox|auto] [--answers rules.json] [--check]
    uv run python -m agent inbox                         questions and approvals waiting for a person
    uv run python -m agent approve|reject <id> [--comment "..."] [--resume]
    uv run python -m agent answer <id> "..." [--resume]
    uv run python -m agent resume <run_id>               carry on a run that waited for a person
    uv run python -m agent show [run_id|last] [--full] [--prompt N]
    uv run python -m agent runs
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from agent.config import ROOT, Settings
from agent.eventlog import EventLog

GOOD = ("verified", "finished")


def _short(args: dict, width: int = 150) -> str:
    s = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False, default=str)}" for k, v in args.items())
    return s if len(s) <= width else s[: width - 1] + "…"


class Live:
    """Renders the event stream as it happens (also used to replay a stored run)."""

    def __init__(self, console: Console, verbose: bool = False):
        self.c, self.verbose = console, verbose

    def __call__(self, run_id: str, kind: str, d: dict) -> None:
        c, e = self.c, escape
        if kind == "run.start":
            c.rule(f"[b]Nova[/b] · run {run_id} · {d.get('model')}")
            c.print(f"[b]Request[/b] from {e(d['requester'])}: {e(d['request'])}")
        elif kind == "run.resume":
            c.rule(f"[b]Nova[/b] · run {run_id} · resumed")
        elif kind == "clock":
            c.print(f"[dim]Business date {d['today']} (from {d['source']})[/]")
        elif kind == "understand.docs":
            c.print(f"[cyan]Procedures chosen:[/] {', '.join(d['chosen']) or '(none)'}"
                    + (" [dim](keyword fallback)[/]" if d.get("fallback") else ""))
        elif kind == "llm.reply":
            c.print(f"[dim]#{d['n']} {d['phase']} · {d['seconds']}s{' · thinking' if d['think'] else ''}"
                    f" · ~{d['est_prompt_tokens']} tok[/]")
            if d.get("content") and (self.verbose or not d.get("tool_calls")):
                c.print(f"   [italic]{e(d['content'][:400])}[/]")
        elif kind == "tool.call":
            c.print(f"  [b]{d['name']}[/]({e(_short(d['arguments']))})")
        elif kind == "tool.result":
            mark = "[green]✓[/]" if d["ok"] else "[red]✗[/]"
            c.print(f"    {mark} {e(d['summary'])}")
            if self.verbose:
                c.print(f"[dim]{e(d['text'][:3000])}[/]")
        elif kind == "browser.post" and d.get("action"):
            c.print(f"    [magenta]→ {d['action']}[/]")
        elif kind == "policy":
            if d["effect"] != "allow":
                c.print(f"    [red]policy: {d['effect']} {d['action']}[/] [dim]({', '.join(d['rules'])})[/]")
            elif d.get("approval"):
                c.print(f"    [magenta]policy: allowed by approval {d['approval']}[/]")
        elif kind == "approval.used":
            c.print(f"    [magenta]approval {d['ref']} used for {e(d['used_by'])}[/]")
        elif kind == "contract.reply":
            c.print(f"[dim]contract · {d['seconds']}s{' · thinking' if d['think'] else ''} · {d['output_tokens']} tok[/]")
        elif kind == "plan.submit":
            c.print(f"[cyan]Contract[/] — {e(d['goal'])}" + (f" [dim]({d['seconds']}s)[/]" if d.get("seconds") else ""))
            for f in d.get("facts") or []:
                c.print(f"   [dim]fact[/] {e(f['name'])} = {e(f['value'])}")
            for crit in d.get("success_criteria") or []:
                c.print(f"   [dim]done when:[/] {e(crit)}")
            for i, s in enumerate(d["steps"], 1):
                c.print(f"   {i}. {e(s)}")
        elif kind == "plan.step_done":
            c.print(f"  [cyan]● step {d['step']} {d['status']}[/]: {e(d['result'][:160])}")
        elif kind == "review":
            for p in d["problems"]:
                c.print(f"    [yellow]checker: {e(p['field'])} = {e(str(p['value']))}, worked out: {e(str(p['checker']))}[/]")
            if not d["problems"]:
                c.print(f"    [dim]checker: ok ({d['seconds']}s)[/]")
        elif kind == "review.override":
            c.print(f"    [yellow]checker overridden (resubmitted unchanged): {e(str(d.get('fields')))}[/]")
        elif kind == "provenance.blocked":
            c.print(f"    [yellow]provenance: not seen anywhere: {e('; '.join(d['values']))}[/]")
        elif kind == "runtime.refused":
            c.print(f"    [yellow]runtime: not repeated ({d['times']} identical calls before)[/]")
        elif kind == "approval.decision":
            c.print(f"  [magenta]Approval {d['ref']}: {d['status']} by {e(d['by'])}[/]")
        elif kind == "human.answer":
            c.print(f"  [yellow]{e(d['by'])} answered:[/] {e(d['answer'][:200])}")
        elif kind == "verify":
            n = d["counts"]
            colour = {"verified": "green", "failed": "red"}.get(d["verdict"], "yellow")
            c.print(f"[{colour}]Verification: {d['verdict']}[/] ({n['pass']} confirmed, {n['fail']} failed, "
                    f"{n['unverified']} not checkable; {d['seconds']}s)")
            for x in d["actions"] + d["criteria"]:
                mark = {"pass": "[green]✓[/]", "fail": "[red]✗[/]"}.get(x["status"], "[dim]–[/]")
                c.print(f"   {mark} {e(str(x.get('what') or x.get('criterion')))[:110]} [dim]{e(str(x.get('detail', '')))[:120]}[/]")
        elif kind == "runtime.warning":
            c.print(f"  [yellow]runtime: {e(d['message'])}[/]")
        elif kind == "runtime.stop":
            c.print(f"[red]Stopped: {e(d['reason'])}[/]")
        elif kind == "error":
            c.print(f"[red]Error ({d.get('where')}): {e(d['message'])}[/]")
        elif kind == "run.pause":
            c.rule(f"[yellow]waiting for {e(d['waiting_for'])} ({d['item']})[/]")
        elif kind == "run.end":
            colour = "green" if d["status"] in GOOD else "red"
            c.rule(f"[{colour}]{d['status']}[/]")
            if d.get("summary"):
                c.print(e(d["summary"]))


def _human(mode: str | None, answers: str | None, log: EventLog, console: Console):
    from agent.human import Inbox, InboxHuman, ScriptedHuman

    mode = mode or ("terminal" if sys.stdin.isatty() else "auto")
    if mode == "auto":
        return ScriptedHuman.from_file(answers)
    return InboxHuman(Inbox(log.db), prompt=mode == "terminal", console=console)


def _runtime(log: EventLog, human, a=None):
    if a is not None and getattr(a, "model", None):
        os.environ["NOVA_LLM_MODEL"] = a.model
    from agent.llm import from_env
    from agent.runtime import Runtime

    s = Settings.from_env()
    if a is not None:
        s.headless = not getattr(a, "headed", False)
        s.today = getattr(a, "today", None) or s.today
        s.max_steps = getattr(a, "max_steps", None) or s.max_steps
        s.think = getattr(a, "think", None) or s.think
    return Runtime(s, from_env(), human, log)


def _after(state, log: EventLog, console: Console, check: bool) -> int:
    run = log.run(state.run_id)
    status = run["status"] if run else "?"
    if status == "not_started":
        console.print("[red]Nova did not start, so nothing was changed. Fix the problem above and run it again.[/]")
        return 2
    run_dir = log.run_dir(state.run_id)
    console.print(f"Run record: {run_dir}  (report.md, evidence.json, facts.json)")
    if status == "waiting":
        from agent.human import Inbox
        runs = [state.run_id] + [i["run_id"] for i in state.work_items if i.get("run_id")]  # a split request's items
        for item in [p for r in runs for p in Inbox(log.db).pending(r)]:
            verb = "approve|reject" if item["kind"] == "approval" else "answer"
            console.print(f"Waiting for {item['who']}: [b]{item['id']}[/] → uv run python -m agent {verb} {item['id']} "
                          + ("..." if verb == "answer" else "") + " --resume")
        return 3
    facts = run_dir / "facts.json"
    if check:
        console.rule("Ground truth: python -m world check")
        subprocess.run([sys.executable, "-m", "world", "check", "--facts", str(facts)], cwd=ROOT)
    else:
        rel = facts.relative_to(ROOT) if facts.is_relative_to(ROOT) else facts
        console.print(f"Grade it:   uv run python -m world check --facts {rel}")
    return 0 if status in GOOD else 1


def _cmd_run(a, console: Console) -> int:
    log = EventLog(Settings.from_env().runs_dir)
    log.subscribe(Live(console, a.verbose))
    rt = _runtime(log, _human(a.human, a.answers, log, console), a)
    return _after(rt.run(a.request, a.requester), log, console, a.check)


def _cmd_resume(a, console: Console) -> int:
    log = EventLog(Settings.from_env().runs_dir)
    run = log.run(a.run_id)
    if not run:
        console.print(f"No run {a.run_id!r}.")
        return 1
    log.subscribe(Live(console, a.verbose))
    rt = _runtime(log, _human(a.human, None, log, console), a)
    try:
        return _after(rt.resume(run["id"]), log, console, a.check)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        return 1


def _cmd_inbox(a, console: Console) -> int:
    from agent.human import Inbox

    items = Inbox(EventLog(Settings.from_env().runs_dir).db).pending()
    if not items:
        console.print("Nothing is waiting for a person.")
        return 0
    t = Table("id", "for", "kind", "run", "what")
    for i in items:
        p = i["payload"]
        what = p["question"] if i["kind"] == "question" else (
            f"{p['kind']}: " + ", ".join(f"{k}={v}" for k, v in p["details"].items()))
        t.add_row(i["id"], i["who"] or "", i["kind"], i["run_id"], what)
    console.print(t)
    return 0


def _decide(a, console: Console, status: str, answer: str) -> int:
    from agent.human import Inbox

    log = EventLog(Settings.from_env().runs_dir)
    inbox = Inbox(log.db)
    item = inbox.get(a.id)
    if not item:
        console.print(f"[red]No inbox item {a.id!r}.[/] See `uv run python -m agent inbox`.")
        return 1
    if (item["kind"] == "approval") != (status in ("approved", "rejected")):
        console.print(f"[red]{a.id} is a {item['kind']}: use " + ("approve/reject" if item["kind"] == "approval"
                                                                 else "answer") + ".[/]")
        return 1
    try:
        inbox.decide(a.id, status, answer, a.by or item["who"] or "",
                     remember=getattr(a, "remember", False) and status != "approved")
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        return 1
    console.print(f"{a.id}: {status}" + (f" ({answer})" if answer else ""))
    run = log.run(item["run_id"])
    if run and run["status"] == "waiting" and not inbox.pending(item["run_id"]):
        if getattr(a, "resume", False):
            a.run_id, a.human, a.check, a.verbose = item["run_id"], "terminal", True, False
            return _cmd_resume(a, console)
        console.print(f"Run {item['run_id']} can carry on: uv run python -m agent resume {item['run_id']}")
    return 0


def _cmd_show(a, console: Console) -> int:
    log = EventLog(Settings.from_env().runs_dir)
    run = log.run(a.run_id)
    if not run:
        console.print(f"No run {a.run_id!r}.")
        return 1
    live = Live(console, a.full)
    for ev in log.events(run["id"]):
        live(run["id"], ev["kind"], ev["data"])
    if a.prompt is not None:
        calls = log.llm_calls(run["id"])
        call = calls[a.prompt - 1] if 0 < a.prompt <= len(calls) else calls[-1]
        for m in call["request"]["messages"]:
            console.rule(m["role"])
            console.print(escape(m.get("content") or ""))
            for tc in m.get("tool_calls") or []:
                console.print(f"[b]→ {tc.get('name')}[/]({escape(json.dumps(tc.get('arguments'), ensure_ascii=False))})")
        console.rule("reply")
        console.print(escape(json.dumps(call["response"], ensure_ascii=False, indent=1)[:4000]))
    return 0


def _cmd_runs(a, console: Console) -> int:
    t = Table("run", "started", "status", "verdict", "request")
    now = datetime.now()
    for r in EventLog(Settings.from_env().runs_dir).runs(a.limit):
        status = r["status"]
        beat = r.get("heartbeat_at") or r["started_at"]
        if status == "running" and (now - datetime.fromisoformat(beat)).total_seconds() > 1800:
            status = "running? (no activity for 30+ min: the process probably died)"
        t.add_row(r["id"], r["started_at"], status, r.get("verdict") or "", r["request"][:50])
    console.print(t)
    return 0


def main() -> int:
    load_dotenv(ROOT / ".env")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m agent", description="Nova, the AI AP associate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    human_help = ("who answers questions and approvals: terminal (ask here, Ctrl+C or 'later' to answer from the "
                  "inbox), inbox (always from the inbox), auto (scripted). Default: terminal when interactive.")
    r = sub.add_parser("run", help="give Nova a request")
    r.add_argument("request")
    r.add_argument("--requester", default="Rahul Mehta (AP Manager)")
    r.add_argument("--human", choices=["terminal", "inbox", "auto"], help=human_help)
    r.add_argument("--answers", help="rules file (JSON) for --human auto")
    r.add_argument("--model", help="override NOVA_LLM_MODEL")
    r.add_argument("--max-steps", type=int)
    r.add_argument("--think", choices=["never", "recovery", "always"],
                   help="when the model may think before answering (default: recovery, after repeated failures)")
    r.add_argument("--today", help="business date DD-MM-YYYY (default: read from the Finance Portal)")
    r.add_argument("--headed", action="store_true", help="show the browser window (watch it; don't click in it)")
    r.add_argument("--check", action="store_true", help="grade the outcome with `python -m world check` afterwards")
    r.add_argument("-v", "--verbose", action="store_true")
    rr = sub.add_parser("resume", help="carry on a run that was waiting for a person")
    rr.add_argument("run_id")
    rr.add_argument("--human", choices=["terminal", "inbox", "auto"], help=human_help)
    rr.add_argument("--headed", action="store_true")
    rr.add_argument("--check", action="store_true")
    rr.add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("inbox", help="questions and approvals waiting for a person")
    for name, help_ in (("approve", "approve a request"), ("reject", "reject a request")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("id")
        p.add_argument("--comment", default="")
        p.add_argument("--by", help="who decides (default: the approver the policy names)")
        p.add_argument("--resume", action="store_true", help="carry on the run straight away")
        p.add_argument("--remember", action="store_true",
                       help="(reject) keep the comment as a standing instruction for later tasks")
    an = sub.add_parser("answer", help="answer a question")
    an.add_argument("id")
    an.add_argument("text")
    an.add_argument("--by")
    an.add_argument("--resume", action="store_true")
    an.add_argument("--remember", action="store_true",
                    help="keep this answer as a standing instruction: the same question later is answered by it")
    sub.add_parser("lessons", help="standing instructions people asked Nova to remember")
    fg = sub.add_parser("forget", help="retire a standing instruction")
    fg.add_argument("id")
    s = sub.add_parser("show", help="replay a run's events")
    s.add_argument("run_id", nargs="?", default="last")
    s.add_argument("--full", action="store_true", help="include full tool outputs")
    s.add_argument("--prompt", type=int, nargs="?", const=0, help="print the exact prompt of model call N (default: last)")
    rs = sub.add_parser("runs", help="list recent runs")
    rs.add_argument("--limit", type=int, default=20)
    a = ap.parse_args()
    console = Console()
    if a.cmd in ("approve", "reject"):
        return _decide(a, console, "approved" if a.cmd == "approve" else "rejected", a.comment)
    if a.cmd == "answer":
        return _decide(a, console, "answered", a.text)
    return {"run": _cmd_run, "resume": _cmd_resume, "inbox": _cmd_inbox, "show": _cmd_show,
            "runs": _cmd_runs, "lessons": _cmd_lessons, "forget": _cmd_forget}[a.cmd](a, console)


def _cmd_lessons(a, console: Console) -> int:
    from agent.memory import Lessons

    lessons = Lessons(EventLog(Settings.from_env().runs_dir).db).all()
    if not lessons:
        console.print("No standing instructions yet. Answer a question with --remember (or say yes when asked) to "
                      "add one.")
        return 0
    t = Table("id", "status", "from", "when", "situation", "instruction", "used")
    for x in lessons:
        t.add_row(x.id, x.status, x.by, x.created_at[:10], x.situation[:70], x.guidance[:70], str(x.uses))
    console.print(t)
    return 0


def _cmd_forget(a, console: Console) -> int:
    from agent.memory import Lessons

    ok = Lessons(EventLog(Settings.from_env().runs_dir).db).retire(a.id)
    console.print(f"{a.id} retired: it is no longer used." if ok else f"[red]No active standing instruction {a.id}.[/]")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
