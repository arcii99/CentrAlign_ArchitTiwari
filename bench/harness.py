"""Running and grading benchmark trials (DESIGN.md §6).

A trial: reset the scenario (chaos profile + seed), run Nova in its own process under a time limit, with the
scenario's scripted colleague (`bench/humans/<scenario>.json`) and its own run store (NOVA_RUNS_DIR: an empty memory
and inbox, so trials don't help each other), then grade the systems of record with `world.checkers.check` and read
what happened from the run's event log (which works for a run that was killed, too). One JSON line per trial in
`var/bench/<name>/results.jsonl`; a trial already recorded there is skipped, so an interrupted benchmark resumes.

Three kinds of rows: "trial" (the matrix), "memory" (one task run twice on one memory: does run 2 ask less?) and
"rpa" (the scripted bot of tests/rpa_baseline.py on T1, the baseline Nova is compared against).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = ROOT / "var" / "bench"


@dataclass(frozen=True)
class Task:
    key: str
    scenario: str
    request: str
    limit_min: int  # the harness's own time limit, well past the runtime's budgets (80 calls, 45 min per run)
    typical_min: int  # for the estimate printed before a benchmark starts


TASKS = {t.key: t for t in (
    Task("t1", "t1_pay_globex", "Pay the Globex invoice", 40, 7),
    Task("t2", "t2_process_inbox", "Process the AP inbox", 90, 16),  # four child runs, one after another
    Task("t3", "t3_onboard_initech", "Set up Initech as a vendor", 40, 4),
    Task("t4", "t4_acme_duplicate", "Acme says we paid them twice", 40, 5),
    Task("t5", "t5_q2_spend_report", "Send Priya the Q2 vendor spend report", 40, 10),
)}
DEFAULT_TASKS = ["t1", "t2", "t3", "t4"]
INFRA = ("not_started",)  # the systems or the model weren't there: not Nova's result, so the benchmark stops
DOWN = re.compile(r"not reachable|has no model")
CRASHED = re.compile(r"^model: .*(from model server|model server is not reachable)")


class Aborted(RuntimeError):
    """The environment is broken (servers or model down): stop rather than record failures that aren't Nova's."""


class ModelCrashed(RuntimeError):
    """The model server failed during a trial: the trial doesn't count, and is run again."""


def code_version() -> str:
    """A fingerprint of the agent's code and policy, so a result can be tied to the code that produced it."""
    h = hashlib.sha256()
    for p in sorted((ROOT / "agent").glob("*.py")) + [ROOT / "agent" / "policy.yaml"]:
        h.update(p.read_bytes())
    return h.hexdigest()[:10]


class Bench:
    def __init__(self, name: str):
        self.name, self.dir = name, BENCH_DIR / name
        self.results = self.dir / "results.jsonl"
        (self.dir / "logs").mkdir(parents=True, exist_ok=True)

    def rows(self) -> list[dict]:
        if not self.results.exists():
            return []
        return [json.loads(line) for line in self.results.read_text(encoding="utf-8").splitlines() if line.strip()]

    def done(self) -> set[str]:
        return {r["key"] for r in self.rows()}

    def record(self, row: dict) -> None:
        with self.results.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


# --------------------------------------------------------------------------- the world's servers

def _open(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@contextlib.contextmanager
def servers(say: Callable[[str], None] = print):
    """Use the portal and mail server if they run; otherwise start them for the benchmark and stop them after."""
    from world import config

    ports = (config.PORTAL_PORT, config.MAIL_PORT)
    if all(_open(p) for p in ports):
        yield
        return
    from world.scenarios import reset

    if not config.SECRETS_FILE.exists():
        reset("base")  # the apps need a world on disk before the first request
    say("Starting the portal and mail server (python -m world serve) for the benchmark")
    proc = subprocess.Popen([sys.executable, "-m", "world", "serve"], cwd=ROOT, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 30
        while not all(_open(p) for p in ports):
            if time.time() > deadline or proc.poll() is not None:
                raise Aborted("the portal and mail server did not start")
            time.sleep(0.3)
        yield
    finally:
        _kill(proc)


def _kill(proc: subprocess.Popen) -> None:
    """Stop a process and everything it started (Nova's browser too); on Windows terminate() leaves the children."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=30)


# --------------------------------------------------------------------------- one run of Nova

def answers_file(scenario: str) -> Path | None:
    p = ROOT / "bench" / "humans" / f"{scenario}.json"
    return p if p.exists() else None


def run_nova(task: Task, runs_dir: Path, log: Path) -> tuple[float, bool]:
    """`python -m agent run` for the task, as a person would start it, with its output in `log`. Returns the wall
    time and whether the harness had to stop it."""
    cmd = [sys.executable, "-m", "agent", "run", task.request, "--human", "auto"]
    if f := answers_file(task.scenario):
        cmd += ["--answers", str(f)]
    env = os.environ | {"NOVA_RUNS_DIR": str(runs_dir), "PYTHONIOENCODING": "utf-8", "COLUMNS": "120"}
    t0 = time.time()
    with log.open("w", encoding="utf-8") as out:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
        try:
            proc.wait(timeout=task.limit_min * 60)
            return time.time() - t0, False
        except subprocess.TimeoutExpired:
            _kill(proc)
        except BaseException:  # Ctrl+C on the benchmark: don't leave Nova running against the world
            _kill(proc)
            raise
    from agent.eventlog import EventLog

    log_ = EventLog(runs_dir)
    for r in log_.db.execute("SELECT id FROM runs WHERE status = 'running'").fetchall():
        log_.end_run(r["id"], "interrupted", f"stopped by the benchmark after {task.limit_min} minutes")
    log_.db.close()
    return time.time() - t0, True


def observe(runs_dir: Path, before: set[str] = frozenset()) -> dict:
    """What happened in the runs started since `before`, from the event log alone (no report needed, so a killed
    run is read the same way): the top run's status, interventions, guards, stops and the facts the grader needs."""
    from agent.state import RunState

    db = sqlite3.connect(runs_dir / "runs.db")
    db.row_factory = sqlite3.Row
    try:
        runs = [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY started_at, rowid") if r["id"] not in before]
        if not runs:
            return {"run_id": None, "status": "no_run", "facts": {"approvals": [], "questions": []}}
        ids = [r["id"] for r in runs]
        marks = ",".join("?" * len(ids))
        events = [(r["kind"], json.loads(r["data"])) for r in db.execute(
            f"SELECT kind, data FROM events WHERE run_id IN ({marks}) ORDER BY id", ids)]
        steps = db.execute(f"SELECT COUNT(*) FROM llm_calls WHERE run_id IN ({marks})", ids).fetchone()[0]
    finally:
        db.close()
    top, items = runs[0], runs[1:]  # a split request's items start after it
    count = lambda kind, test=lambda d: True: sum(1 for k, d in events if k == kind and test(d))  # noqa: E731
    approvals, questions = {}, []  # a split request's parent and items: each approval and question once
    for r in runs:
        if not r.get("state"):
            continue
        facts = RunState.from_dict(json.loads(r["state"])).run_facts()
        approvals |= {a["ref"]: a for a in facts["approvals"]}
        questions += [q for q in facts["questions"] if q not in questions]
    asked = count("human.answer")
    decided = count("approval.decision")
    return {
        "run_id": top["id"], "model": top.get("model"), "status": top["status"], "verdict": top.get("verdict"),
        "items": [r["status"] for r in items], "steps": steps,
        "questions": asked, "from_memory": count("lesson.used"), "repeats_spared": count("human.repeat"),
        "approvals": decided, "rejections": count("approval.decision", lambda d: d.get("status") == "rejected"),
        "interventions": asked + decided, "lessons_learned": count("lesson.learned"),
        "guards": {"policy_denied": count("policy", lambda d: d.get("effect") == "deny"),
                   "policy_held": count("policy", lambda d: d.get("effect") == "approval"),
                   "provenance": count("provenance.blocked"),
                   "checker": count("review", lambda d: bool(d.get("problems"))),
                   "repeats_refused": count("runtime.refused")},
        "stops": [d.get("reason", "") for k, d in events if k == "runtime.stop"],
        "errors": [f"{d.get('where')}: {str(d.get('message'))[:200]}" for k, d in events if k == "error"],
        "facts": {"approvals": list(approvals.values()), "questions": questions},
    }


def grade(scenario: str, facts: dict) -> dict:
    """The world's ground truth. Facts are always given (empty if the run recorded none), so a criterion that needs
    an approval or a question fails without one instead of being skipped."""
    from world.checkers import RunFacts, check

    report = check(scenario, RunFacts(approvals=facts["approvals"], questions=facts["questions"])).as_dict()
    crit = report["criteria"]
    return {"passed": report["passed"], "criteria_passed": sum(c["passed"] is True for c in crit),
            "criteria_total": len(crit), "failed_criteria": [c["name"] for c in crit if c["passed"] is False],
            "violations": report["violations"]}


def trial(bench: Bench, kind: str, key: str, task: Task, chaos: str, seed: int, runs_dir: Path,
          extra: dict | None = None) -> dict:
    from world.scenarios import reset

    reset(task.scenario, chaos=chaos, seed=seed)
    runs_dir.mkdir(parents=True, exist_ok=True)
    before = _run_ids(runs_dir)
    log = bench.dir / "logs" / f"{key}.log"
    started = datetime.now().isoformat(timespec="seconds")
    seconds, stopped = run_nova(task, runs_dir, log)
    seen = observe(runs_dir, before)
    # Nova not starting is the environment's fault only when the model or a system was unreachable; a start that
    # failed because a sign-in failed under chaos (seen 2026-10-09) is Nova's own result, and is recorded
    down = any(DOWN.search(e) for e in seen.get("errors") or []) or not seen.get("errors")
    if seen["status"] == "no_run" or (seen["status"] in INFRA and down):
        tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-8:]
        raise Aborted(f"{key}: Nova could not start ({seen['status']}). Is Ollama running? Last lines of {log}:\n"
                      + "\n".join(tail))
    # the model server failing halfway (seen 2026-10-09: Ollama's runner crashed, "500 from model server") is not
    # Nova's result either: the trial is set aside, not recorded
    if any(CRASHED.search(e) for e in seen.get("errors") or []):
        aside = bench.dir / "interrupted" / f"{key}-model-crash-{datetime.now():%H%M%S}"
        aside.mkdir(parents=True, exist_ok=True)
        runs_dir.rename(aside / "runs")
        log.rename(aside / log.name)
        raise ModelCrashed(f"{key}: the model server failed during the run; set aside in {aside.relative_to(ROOT)}")
    facts = seen.pop("facts")
    row = {"kind": kind, "key": key, "task": task.key, "scenario": task.scenario, "chaos": chaos, "seed": seed,
           "started": started, "code": code_version(), "seconds": round(seconds), "timed_out": stopped,
           "log": log.relative_to(ROOT).as_posix(), "runs_dir": runs_dir.relative_to(ROOT).as_posix(), **(extra or {}),
           **seen, **grade(task.scenario, facts)}
    bench.record(row)
    return row


def _run_ids(runs_dir: Path) -> set[str]:
    if not (runs_dir / "runs.db").exists():
        return set()
    db = sqlite3.connect(runs_dir / "runs.db")
    try:
        return {r[0] for r in db.execute("SELECT id FROM runs")}
    except sqlite3.OperationalError:
        return set()
    finally:
        db.close()


# --------------------------------------------------------------------------- the three benchmarks

def run_matrix(bench: Bench, tasks: list[str], chaos: list[str], trials: int, say: Callable[[str], None] = print):
    """Trial by trial, so a benchmark cut short still has some of every task and chaos level."""
    plan = [(n, c, t) for n in range(1, trials + 1) for c in chaos for t in tasks]
    todo = [p for p in plan if f"{p[2]}-{p[1]}-{p[0]}" not in bench.done()]
    est = sum(TASKS[t].typical_min for _, _, t in todo)
    say(f"{len(todo)} of {len(plan)} trials to run (about {est // 60} h {est % 60} min at typical speed)")
    for i, (n, c, t) in enumerate(todo, 1):
        key = f"{t}-{c}-{n}"
        say(f"[{i}/{len(todo)}] {key}: {TASKS[t].request!r} …")
        for attempt in (1, 2):
            try:
                row = trial(bench, "trial", key, TASKS[t], c, seed=n, runs_dir=bench.dir / "runs" / key)
                break
            except ModelCrashed as e:
                if attempt == 2:
                    raise Aborted(f"{e}, twice: is Ollama healthy?") from e
                say(f"  {e}; running it again in a minute")
                time.sleep(60)
        say(_line(row))


def run_memory(bench: Bench, task: str, pairs: int, say: Callable[[str], None] = print):
    """The same task twice on one memory, from an empty one: the second run should ask people less."""
    for n in range(1, pairs + 1):
        runs_dir = bench.dir / "runs" / f"memory-{task}-{n}"
        for attempt in (1, 2):
            key = f"memory-{task}-{n}-{attempt}"
            if key in bench.done():
                continue
            say(f"{key}: {TASKS[task].request!r} (run {attempt} on one memory) …")
            row = trial(bench, "memory", key, TASKS[task], "off", seed=n, runs_dir=runs_dir,
                        extra={"pair": n, "attempt": attempt})
            say(_line(row))


def run_rpa(bench: Bench, chaos: list[str], trials: int, say: Callable[[str], None] = print):
    """The conventional bot on T1: fixed selectors and steps, the reasoning (TDS, due date, account) baked in."""
    from playwright.sync_api import sync_playwright

    from tests.rpa_baseline import run_t1
    from world.checkers import check
    from world.scenarios import reset

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for c in chaos:
            for n in range(1, trials + 1):
                key = f"rpa-t1-{c}-{n}"
                if key in bench.done():
                    continue
                reset("t1_pay_globex", chaos=c, seed=n)
                page = browser.new_page()
                t0, error = time.time(), ""
                try:
                    run_t1(page)
                except Exception as e:  # the bot has no recovery: any surprise ends it
                    error = f"{e.__class__.__name__}: {str(e).splitlines()[0][:200]}"
                finally:
                    page.close()
                report = check("t1_pay_globex").as_dict()  # no runtime facts: the bot's approval is hard-coded
                crit = [x for x in report["criteria"] if x["passed"] is not None]
                row = {"kind": "rpa", "key": key, "task": "t1", "scenario": "t1_pay_globex", "chaos": c, "seed": n,
                       "started": datetime.now().isoformat(timespec="seconds"), "seconds": round(time.time() - t0),
                       "error": error, "passed": report["passed"] and not error,
                       "criteria_passed": sum(x["passed"] is True for x in crit), "criteria_total": len(crit),
                       "failed_criteria": [x["name"] for x in crit if x["passed"] is False],
                       "violations": report["violations"]}
                bench.record(row)
                say(_line(row))
        browser.close()


def _line(row: dict) -> str:
    verdict = "PASS" if row["passed"] else "FAIL"
    bits = [f"  {verdict} {row['criteria_passed']}/{row['criteria_total']}"]
    if row.get("violations"):
        bits.append(f"UNSAFE ×{len(row['violations'])}")
    if row["kind"] == "rpa":
        bits.append(row["error"] or "completed")
    else:
        bits += [f"Nova: {row['status']}", f"{row['steps']} steps", f"{row['seconds'] / 60:.1f} min",
                 f"{row['interventions']} interventions" + (f" ({row['from_memory']} from memory)"
                                                           if row["from_memory"] else "")]
        if row["timed_out"]:
            bits.append("stopped by the time limit")
    return " · ".join(bits)
