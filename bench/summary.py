"""Turning results.jsonl into numbers: summary.json (for the dashboard) and summary.md (for people).

Success is the world's ground truth only: every graded criterion passed and no safety invariant broken. Nova's own
verdict is reported next to it, as a claim to be checked: "verified" on a failed task is the worst outcome a
trustworthy agent can have, so it gets its own count.
"""

from __future__ import annotations

import json
from collections import Counter
from statistics import median

from bench.harness import TASKS, Bench

GOOD = ("verified", "finished")


def _cell(rows: list[dict], full: dict[str, int] | None = None) -> dict:
    """`full`: each task's number of criteria when everything is there. The grader checks fewer when a run fails
    early (no bill, so nothing about its payment), and those runs must not look better for it."""
    n = len(rows)
    passed = sum(r["passed"] for r in rows)
    claims = [r for r in rows if r["status"] == "verified"]
    full = full or {}
    out_of = sum(max(r["criteria_total"], full.get(r["task"], 0)) for r in rows)
    return {
        "trials": n, "passed": passed, "rate": round(passed / n, 3) if n else None,
        "all_passed": n > 0 and passed == n,  # pass^k: every one of the k trials
        "criteria": round(sum(r["criteria_passed"] for r in rows) / max(1, out_of), 3),
        "unsafe": sum(bool(r["violations"]) for r in rows),
        "interventions": round(sum(r["interventions"] for r in rows) / n, 2) if n else None,
        "steps": median(r["steps"] for r in rows) if n else None,
        "minutes": round(median(r["seconds"] for r in rows) / 60, 1) if n else None,
        "statuses": dict(Counter(r["status"] for r in rows)),
        "verified": len(claims), "verified_but_failed": sum(not r["passed"] for r in claims),
        "passed_unclaimed": sum(r["passed"] and r["status"] != "verified" for r in rows),
    }


def summarize(rows: list[dict]) -> dict:
    trials = [r for r in rows if r["kind"] == "trial"]
    cells = {}
    for r in trials:
        cells.setdefault(f"{r['task']}|{r['chaos']}", []).append(r)
    by_task = {}
    for r in trials:
        by_task.setdefault(r["task"], []).append(r)
    memory = {}
    for r in (r for r in rows if r["kind"] == "memory"):
        memory.setdefault(r["pair"], {})[r["attempt"]] = r
    rpa = {}
    for r in (r for r in rows if r["kind"] == "rpa"):
        rpa.setdefault(r["chaos"], []).append(r)
    nova_t1 = {c.split("|")[1]: _cell(v) for c, v in cells.items() if c.startswith("t1|")}  # rates only
    guards = Counter()
    for r in trials:
        guards.update(r.get("guards") or {})
    full: dict[str, int] = {}
    for r in rows:
        full[r["task"]] = max(full.get(r["task"], 0), r["criteria_total"])
    return {
        "requests": {k: t.request for k, t in TASKS.items()},
        "overall": _cell(trials, full) if trials else None,
        "models": sorted({r.get("model") or "?" for r in trials}),
        "code": sorted({r.get("code") for r in rows if r.get("code")}),
        "first": min((r["started"] for r in rows), default=None), "last": max((r["started"] for r in rows), default=None),
        "cells": {k: _cell(v, full) for k, v in sorted(cells.items())},
        "tasks": {k: _cell(v, full) for k, v in sorted(by_task.items())},
        "guards": dict(guards),
        "memory": [{"pair": p, **{f"run{a}": {k: r[k] for k in ("passed", "questions", "from_memory", "approvals",
                                                                   "interventions", "lessons_learned", "status")}
                                  for a, r in sorted(runs.items())}} for p, runs in sorted(memory.items())],
        "rpa": {c: {"trials": len(v), "passed": sum(r["passed"] for r in v),
                    "errors": [r["error"] for r in v if r["error"]], "unsafe": sum(bool(r["violations"]) for r in v)}
                for c, v in rpa.items()},
        "nova_t1": nova_t1,
        "failures": [{k: r.get(k) for k in ("key", "status", "criteria_passed", "criteria_total", "failed_criteria",
                                             "violations", "stops", "errors", "timed_out", "log")}
                     for r in trials if not r["passed"]],
    }


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:.0f}%"


def markdown(name: str, s: dict) -> str:
    out = [f"# Benchmark `{name}`", ""]
    o = s["overall"]
    if o:
        out += [f"Model {', '.join(s['models'])}; code {', '.join(s['code'])}; {s['first']} → {s['last']}.", "",
                f"**{o['passed']} of {o['trials']} trials passed the ground-truth grader ({_pct(o['rate'])}). "
                f"Unsafe trials: {o['unsafe']}. Nova said \"verified\" {o['verified']} times, wrongly "
                f"{o['verified_but_failed']} times.**", "",
                "| Task | Chaos | Passed | All k passed | Criteria met | Unsafe | People interrupted (mean) | "
                "Steps (median) | Minutes (median) | How runs ended |",
                "|---|---|---|---|---|---|---|---|---|---|"]
        for key, c in s["cells"].items():
            task, chaos = key.split("|")
            ends = ", ".join(f"{k} {v}" for k, v in sorted(c["statuses"].items(), key=lambda kv: -kv[1]))
            out.append(f"| {task.upper()} {TASKS[task].request} | {chaos} | {c['passed']}/{c['trials']} | "
                       f"{'yes' if c['all_passed'] else 'no'} | {_pct(c['criteria'])} | {c['unsafe']} | "
                       f"{c['interventions']} | {c['steps']} | {c['minutes']} | {ends} |")
        out += ["", "## Does Nova know when it has succeeded?", "",
                "| | Ground truth: passed | Ground truth: failed |", "|---|---|---|",
                f"| Nova said verified | {o['verified'] - o['verified_but_failed']} | {o['verified_but_failed']} |",
                f"| Nova said anything else | {o['passed_unclaimed']} | "
                f"{o['trials'] - o['verified'] - o['passed_unclaimed']} |", ""]
        if s["guards"]:
            g = s["guards"]
            out += ["## Guards that fired", "",
                    f"Across all trials: policy refused {g.get('policy_denied', 0)} actions and held "
                    f"{g.get('policy_held', 0)} until approved; provenance blocked {g.get('provenance', 0)} values "
                    f"that weren't in any document; the checker objected {g.get('checker', 0)} times; "
                    f"{g.get('repeats_refused', 0)} identical repeats were refused.", ""]
    if s["rpa"]:
        out += ["## Nova vs a scripted bot (T1)", "",
                "The bot (`tests/rpa_baseline.py`) has fixed selectors and steps, with the reasoning (TDS, due date, "
                "bank account) worked out by a person beforehand and its approval reference hard-coded, so its "
                "approval criterion is not graded.", "",
                "| Chaos | Bot passed | Nova passed | Bot errors |", "|---|---|---|---|"]
        for c, b in s["rpa"].items():
            n = s["nova_t1"].get(c)
            nova = f"{n['passed']}/{n['trials']}" if n else "not run"
            errs = "; ".join(sorted(set(e.split(":")[0] for e in b["errors"]))) or "–"
            out.append(f"| {c} | {b['passed']}/{b['trials']} | {nova} | {errs} |")
        out.append("")
    if s["memory"]:
        out += ["## Memory: the same task twice, from an empty memory", "",
                "| Pair | Run 1: passed, questions to people, approvals | Lessons kept | "
                "Run 2: passed, questions to people, answered from memory, approvals |", "|---|---|---|---|"]
        for m in s["memory"]:
            a, b = m.get("run1"), m.get("run2")
            one = f"{'pass' if a['passed'] else 'fail'}, {a['questions']}, {a['approvals']}" if a else "–"
            two = (f"{'pass' if b['passed'] else 'fail'}, {b['questions']}, {b['from_memory']}, {b['approvals']}"
                   if b else "–")
            out.append(f"| {m['pair']} | {one} | {a['lessons_learned'] if a else '–'} | {two} |")
        out.append("")
    if s["failures"]:
        out += ["## Failed trials", ""]
        for f in s["failures"]:
            why = "; ".join(f["failed_criteria"] + [f"UNSAFE: {v}" for v in f["violations"]]) or "–"
            how = "; ".join((f["stops"] or []) + (f["errors"] or [])) or ("time limit" if f["timed_out"] else "")
            out.append(f"- `{f['key']}` ({f['status']}, {f['criteria_passed']}/{f['criteria_total']}): {why}"
                       + (f". Stopped: {how}" if how else "") + f". Log: `{f['log']}`")
        out.append("")
    return "\n".join(out)


def write(bench: Bench) -> dict:
    s = summarize(bench.rows())
    (bench.dir / "summary.json").write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    (bench.dir / "summary.md").write_text(markdown(bench.name, s), encoding="utf-8")
    return s
