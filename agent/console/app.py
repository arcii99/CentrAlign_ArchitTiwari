"""Nova's console: `uv run python -m agent console` → http://127.0.0.1:8103

The web face of what the command line already does, for the people around Nova:
  /                 runs (newest first) and a box to give Nova a request
  /runs/<id>        one run: its contract and plan, every step as it happens, the checks, the evidence report
  /inbox            questions and approvals waiting for a person: answer, approve or reject (and "remember this");
                    when nothing is left open for a run, it carries on by itself
  /lessons          standing instructions people asked Nova to remember; forget one
  /bench            benchmark results (var/bench/<name>/summary.json)

It reads Nova's records and writes only what a person decides (inbox, lessons). Runs it starts or resumes are
ordinary `python -m agent run|resume --human inbox` processes, so every request reaches a person through the inbox.
There is no login: it is a local tool, like the CLI. The world's systems are not touched from here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from agent.config import ROOT, Settings
from agent.eventlog import EventLog
from agent.human import Inbox
from agent.memory import Lessons

HERE = Path(__file__).parent
BENCH_DIR = ROOT / "var" / "bench"
env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
app = FastAPI(title="Nova console", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

GOOD = ("verified", "finished")
BAD = ("abandoned", "failed", "verification_failed", "stuck", "out_of_steps", "out_of_time", "blocked", "crashed", "model_error",
       "interrupted", "incomplete", "not_started")


@contextmanager
def _log() -> Iterator[EventLog]:
    log = EventLog(Settings.from_env().runs_dir)
    try:
        yield log
    finally:
        log.db.close()


def _page(template: str, **ctx) -> HTMLResponse:
    with _log() as log:
        waiting = len(Inbox(log.db).pending())
    return HTMLResponse(env.get_template(template).render(waiting=waiting, tone=tone, **ctx))


def tone(status: str | None) -> str:
    return "good" if status in GOOD else "bad" if status in BAD else "wait" if status in ("waiting", "running") else ""


def _minutes(run: dict) -> str:
    try:
        end = datetime.fromisoformat(run["ended_at"]) if run.get("ended_at") else datetime.now()
        return f"{(end - datetime.fromisoformat(run['started_at'])).total_seconds() / 60:.1f}"
    except (TypeError, ValueError):
        return ""


def _short(args: dict, width: int = 220) -> str:
    s = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False, default=str)}" for k, v in args.items())
    return s if len(s) <= width else s[: width - 1] + "…"


def _start(args: list[str], runs_dir: Path, tag: str) -> None:
    """A run in its own process, like one started from a terminal; its output goes to a file in the runs folder."""
    out = (runs_dir / f"console-{tag}-{datetime.now():%Y%m%d-%H%M%S}.log").open("w", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([sys.executable, "-m", "agent", *args, "--human", "inbox"], cwd=ROOT, stdout=out,
                     stderr=subprocess.STDOUT, env=os.environ | {"PYTHONIOENCODING": "utf-8"}, creationflags=flags)


# --------------------------------------------------------------------------- the timeline of a run

def timeline(events: list[dict]) -> list[dict]:
    """Events as rows a person can follow: each tool call with its result, every decision by a guard or a person."""
    rows: list[dict] = []
    step, last_tool = 0, None

    def row(cls: str, title: str, body: str = "", at: str = "") -> dict:
        r = {"cls": cls, "title": title, "body": body, "at": at, "step": ""}
        rows.append(r)
        return r

    for ev in events:
        k, d, at = ev["kind"], ev["data"], ev["at"][11:19]
        if k == "llm.reply":
            step = d.get("n", step)
        elif k == "tool.call":
            last_tool = row("tool", d["name"], _short(d.get("arguments") or {}), at)
            last_tool["step"] = step
        elif k == "tool.result" and last_tool is not None:
            last_tool["result"], last_tool["ok"] = d.get("summary", ""), d.get("ok")
        elif k == "browser.post" and d.get("action") and last_tool is not None:
            last_tool["action"] = d["action"]
        elif k == "policy" and d.get("effect") != "allow":
            row("guard", f"Policy: {d['effect']} for {d['action']}", "; ".join(d.get("rules") or []), at)
        elif k == "policy" and d.get("approval"):
            row("policy", f"Allowed by approval {d['approval']}", d.get("action", ""), at)
        elif k == "approval.request":
            row("human", f"Approval requested from {d.get('approver')}: {d.get('kind', '')}",
                _short(d.get("details") or {}), at)
        elif k == "approval.decision":
            row("human" if d.get("status") == "approved" else "guard",
                f"Approval {d['ref']} {d['status']} by {d.get('by')}", d.get("comment", ""), at)
        elif k == "human.question":
            row("human", f"Question to {d.get('to')} ({d.get('key')})", d.get("question", ""), at)
        elif k == "human.answer":
            row("human", f"{d.get('by')} answered", d.get("answer", ""), at)
        elif k == "human.repeat":
            row("memory", "Not asked twice: the earlier answer was used", d.get("answer", ""), at)
        elif k == "lesson.recalled":
            row("memory", "Standing instructions recalled for this task", "\n".join(d.get("lessons") or []), at)
        elif k == "lesson.used":
            row("memory", f"Answered by standing instruction {d.get('id')} ({d.get('by')}); nobody interrupted",
                d.get("guidance", ""), at)
        elif k == "lesson.learned":
            row("memory", f"Learned standing instruction {d.get('id')} from {d.get('by')}", d.get("guidance", ""), at)
        elif k == "review":
            if d.get("problems"):
                row("guard", "Checker objected (re-derived with tools)", "; ".join(
                    f"{p['field']} = {p['value']}, worked out: {p['checker']}" for p in d["problems"]), at)
            else:
                row("policy", "Checker re-derived the values: they agree", "", at)
        elif k == "review.override":
            row("guard", "Checker overridden (resubmitted unchanged)", str(d.get("fields", "")), at)
        elif k == "provenance.blocked":
            row("guard", "Provenance: values not seen in any document or page", "; ".join(d.get("values") or []), at)
        elif k == "runtime.refused":
            row("guard", f"Repeat refused ({d.get('times')} identical calls before)", d.get("name", ""), at)
        elif k == "understand.docs":
            row("plan", "Handbook procedures chosen", ", ".join(d.get("chosen") or []), at)
        elif k == "plan.submit":
            row("plan", "Contract written", d.get("goal", ""), at)
        elif k == "plan.step_done":
            row("plan", f"Step {d.get('step')} {d.get('status')}", d.get("result", ""), at)
        elif k == "verify":
            n = d.get("counts") or {}
            row("good" if d.get("verdict") == "verified" else "bad" if d.get("verdict") == "failed" else "plan",
                f"Independent verification: {d.get('verdict')}",
                f"{n.get('pass', 0)} confirmed, {n.get('fail', 0)} failed, {n.get('unverified', 0)} not checkable", at)
        elif k == "runtime.warning":
            row("muted", "Runtime", d.get("message", ""), at)
        elif k == "runtime.stop":
            row("bad", "Stopped", d.get("reason", ""), at)
        elif k == "error":
            row("bad", f"Error ({d.get('where')})", str(d.get("message", "")), at)
        elif k == "run.pause":
            row("wait", f"Paused: waiting for {d.get('waiting_for')} ({d.get('item')})", "", at)
        elif k in ("run.start", "run.resume"):
            row("muted", "Run started" if k == "run.start" else "Run resumed", d.get("request", ""), at)
        elif k == "run.end":
            row(tone(d.get("status")) or "muted", f"Run ended: {d.get('status')}", d.get("summary", ""), at)
    return rows


# --------------------------------------------------------------------------- pages

@app.get("/", response_class=HTMLResponse)
def runs_page(limit: int = 50):
    with _log() as log:
        runs = log.runs(limit)
        for r in runs:
            state = json.loads(r["state"]) if r.get("state") else {}
            r["parent"], r["minutes"] = state.get("parent", ""), _minutes(r)
            r["steps"] = log.db.execute("SELECT COUNT(*) FROM llm_calls WHERE run_id = ?", (r["id"],)).fetchone()[0]
    for r in runs:  # like `agent runs`: a run whose heartbeat stopped long ago was abandoned (its process died)
        beat = r.get("heartbeat_at") or r["started_at"]
        if r["status"] == "running" and (datetime.now() - datetime.fromisoformat(beat)).total_seconds() > 1800:
            r["status"] = "abandoned"
    running = [r for r in runs if r["status"] == "running"]
    return _page("runs.html", runs=runs, running=running)


@app.post("/runs")
def start_run(request: str = Form(...), requester: str = Form("Rahul Mehta (AP Manager)"), headed: str = Form("")):
    request = request.strip()
    if not request:
        return RedirectResponse("/", status_code=303)
    s = Settings.from_env()
    with _log() as log:
        before = {r["id"] for r in log.runs(5)}
    _start(["run", request, "--requester", requester] + (["--headed"] if headed else []), s.runs_dir, "run")
    for _ in range(40):  # the run's record appears within a few seconds (after the model and systems check)
        time.sleep(0.5)
        with _log() as log:
            new = [r for r in log.runs(5) if r["id"] not in before]
            if new and headed:  # resumed runs show their browser too
                (log.run_dir(new[-1]["id"]) / "console.json").write_text(json.dumps({"headed": True}))
        if new:
            return RedirectResponse(f"/runs/{new[-1]['id']}", status_code=303)
    return RedirectResponse("/", status_code=303)


def _resume(run_id: str) -> None:
    runs_dir = Settings.from_env().runs_dir
    prefs = runs_dir / run_id / "console.json"
    headed = prefs.exists() and json.loads(prefs.read_text()).get("headed")
    _start(["resume", run_id] + (["--headed"] if headed else []), runs_dir, "resume")


def _run_view(log: EventLog, run_id: str) -> dict:
    run = log.run(run_id)
    if not run:
        raise HTTPException(404, f"no run {run_id}")
    state = json.loads(run["state"]) if run.get("state") else {}
    events = log.events(run["id"])
    verify = next((e["data"] for e in reversed(events) if e["kind"] == "verify"), None)
    items = []
    for item in state.get("work_items") or []:
        child = log.run(item["run_id"]) if item.get("run_id") else None
        items.append(item | {"status": child["status"] if child else item.get("status")})
    files = [f for f in ("report.md", "evidence.json", "facts.json") if (log.run_dir(run["id"]) / f).exists()]
    return {"run": run | {"minutes": _minutes(run)}, "state": state, "rows": timeline(events), "verify": verify,
            "items": items, "files": files, "pending": Inbox(log.db).pending(run["id"]),
            "last": events[-1]["seq"] if events else 0,
            "steps": log.db.execute("SELECT COUNT(*) FROM llm_calls WHERE run_id = ?", (run["id"],)).fetchone()[0]}


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(run_id: str):
    with _log() as log:
        view = _run_view(log, run_id)
    return _page("run.html", **view)


@app.get("/runs/{run_id}/live")
def run_live(run_id: str, after: int = 0):
    """For the run page while it runs: the timeline again if anything happened since `after`."""
    with _log() as log:
        view = _run_view(log, run_id)
    html = env.get_template("timeline.html").render(rows=view["rows"]) if view["last"] != after else ""
    return JSONResponse({"last": view["last"], "status": view["run"]["status"], "html": html,
                         "steps": view["steps"], "minutes": view["run"]["minutes"]})


@app.get("/runs/{run_id}/files/{name}", response_class=PlainTextResponse)
def run_file(run_id: str, name: str):
    if name not in ("report.md", "evidence.json", "facts.json"):
        raise HTTPException(404)
    with _log() as log:
        run = log.run(run_id)
        path = log.run_dir(run["id"]) / name if run else None
    if not path or not path.exists():
        raise HTTPException(404)
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")


@app.post("/runs/{run_id}/resume")
def resume_run(run_id: str):
    with _log() as log:
        run = log.run(run_id)
        if not run or run["status"] != "waiting" or Inbox(log.db).pending(run["id"]):
            return RedirectResponse(f"/runs/{run_id}", status_code=303)
    _resume(run["id"])
    time.sleep(1.5)
    return RedirectResponse(f"/runs/{run['id']}", status_code=303)


@app.get("/inbox", response_class=HTMLResponse)
def inbox_page():
    with _log() as log:
        items = Inbox(log.db).pending()
        for i in items:
            run = log.run(i["run_id"])
            i["request"] = run["request"] if run else ""
    return _page("inbox.html", items=items)


@app.post("/inbox/{item_id}")
def decide(item_id: str, action: str = Form(...), text: str = Form(""), remember: str = Form(""),
           back: str = Form("/inbox")):
    """A person's decision, as `agent approve|reject|answer` records it; the run carries on when nothing is left."""
    status = {"approve": "approved", "reject": "rejected", "answer": "answered"}.get(action)
    with _log() as log:
        inbox = Inbox(log.db)
        item = inbox.get(item_id)
        if not item or not status or (item["kind"] == "approval") != (status != "answered"):
            raise HTTPException(400, "not a decision this item can take")
        if status == "answered" and not text.strip():
            raise HTTPException(400, "an answer needs text")
        try:
            inbox.decide(item_id, status, text.strip(), item["who"] or "", remember=bool(remember) and status != "approved")
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        run = log.run(item["run_id"])
        carry_on = run and run["status"] == "waiting" and not inbox.pending(item["run_id"])
    if carry_on:  # an item of a split request carries on its parent's remaining items when it ends
        _resume(run["id"])
        time.sleep(1.5)
        return RedirectResponse(f"/runs/{run['id']}", status_code=303)
    return RedirectResponse(back if back.startswith("/") else "/inbox", status_code=303)


@app.get("/lessons", response_class=HTMLResponse)
def lessons_page():
    with _log() as log:
        lessons = Lessons(log.db).all()
    return _page("lessons.html", lessons=lessons)


@app.post("/lessons/{lesson_id}/forget")
def forget(lesson_id: str):
    with _log() as log:
        Lessons(log.db).retire(lesson_id)
    return RedirectResponse("/lessons", status_code=303)


@app.get("/bench", response_class=HTMLResponse)
def bench_list():
    found = []
    for p in sorted(BENCH_DIR.glob("*/summary.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        s = json.loads(p.read_text(encoding="utf-8"))
        found.append({"name": p.parent.name, "overall": s.get("overall"), "last": s.get("last"),
                      "models": s.get("models")})
    return _page("bench_list.html", benches=found)


@app.get("/bench/{name}", response_class=HTMLResponse)
def bench_page(name: str):
    path = BENCH_DIR / name / "summary.json"
    if "/" in name or "\\" in name or not path.exists():
        raise HTTPException(404)
    s = json.loads(path.read_text(encoding="utf-8"))
    results = BENCH_DIR / name / "results.jsonl"
    rows = [json.loads(x) for x in results.read_text(encoding="utf-8").splitlines() if x.strip()] if results.exists() else []
    return _page("bench.html", name=name, s=s, rows=rows, tasks=s.get("requests") or {})


@app.get("/bench/{name}/logs/{key}", response_class=PlainTextResponse)
def bench_log(name: str, key: str):
    path = BENCH_DIR / name / "logs" / f"{key}.log"
    if any(c in name + key for c in "/\\") or not path.exists():
        raise HTTPException(404)
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"), media_type="text/plain; charset=utf-8")
