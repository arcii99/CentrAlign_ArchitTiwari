"""Two benchmarks side by side ("before" and "after" a round of fixes): `python -m bench compare r1 r2`.

Only the trials both benchmarks recorded are compared (same task, chaos level and seed), so a benchmark that was cut
short doesn't flatter or penalise either side. Writes var/bench/compare-<a>-<b>.md.
"""

from __future__ import annotations

from bench.harness import BENCH_DIR, TASKS, Bench
from bench.summary import _cell, _pct


def _trials(names: str) -> list[dict]:
    return [r for n in names.split("+") for r in Bench(n).rows() if r["kind"] == "trial"]


def pooled(a: str, b: str) -> str:
    """Benchmarks pooled ("r2+r3"): every trial counts, unpaired. Small cells (3 trials) move by one trial from run to
    run, so the same code measured twice is the fairer "after"."""
    ta, tb = _trials(a), _trials(b)
    full: dict[str, int] = {}
    for r in ta + tb:
        full[r["task"]] = max(full.get(r["task"], 0), r["criteria_total"])
    ca, cb = _cell(ta, full), _cell(tb, full)
    out = [f"# `{a}` vs `{b}` (pooled)", "", f"| | `{a}` | `{b}` |", "|---|---|---|",
           f"| Trials | {ca['trials']} | {cb['trials']} |",
           f"| Passed the ground-truth grader | **{ca['passed']}/{ca['trials']}** ({_pct(ca['rate'])}) | "
           f"**{cb['passed']}/{cb['trials']}** ({_pct(cb['rate'])}) |",
           f"| Criteria met | {_pct(ca['criteria'])} | {_pct(cb['criteria'])} |",
           f"| Unsafe trials | {ca['unsafe']} | {cb['unsafe']} |",
           f"| Nova said \"verified\" and was wrong | {ca['verified_but_failed']} of {ca['verified']} | "
           f"{cb['verified_but_failed']} of {cb['verified']} |",
           f"| Nova didn't claim a trial that passed | {ca['passed_unclaimed']} | {cb['passed_unclaimed']} |",
           f"| People interrupted per trial (mean) | {ca['interventions']} | {cb['interventions']} |",
           f"| Steps / minutes per trial (median) | {ca['steps']} / {ca['minutes']} | {cb['steps']} / {cb['minutes']} |",
           "", "| Task | Chaos | `" + a + "` passed | `" + b + "` passed |", "|---|---|---|---|"]
    for cell in sorted({f"{r['task']}|{r['chaos']}" for r in ta + tb}):
        task, chaos = cell.split("|")
        xa = [r for r in ta if r["task"] == task and r["chaos"] == chaos]
        xb = [r for r in tb if r["task"] == task and r["chaos"] == chaos]
        out.append(f"| {task.upper()} {TASKS[task].request} | {chaos} | {sum(r['passed'] for r in xa)}/{len(xa)} | "
                   f"{sum(r['passed'] for r in xb)}/{len(xb)} |")
    return "\n".join(out) + "\n"


def compare(a: str, b: str) -> str:
    if "+" in a or "+" in b:
        return pooled(a, b)
    ra = {r["key"]: r for r in Bench(a).rows() if r["kind"] == "trial"}
    rb = {r["key"]: r for r in Bench(b).rows() if r["kind"] == "trial"}
    keys = [k for k in rb if k in ra]
    full: dict[str, int] = {}
    for r in list(ra.values()) + list(rb.values()):
        full[r["task"]] = max(full.get(r["task"], 0), r["criteria_total"])
    ca, cb = _cell([ra[k] for k in keys], full), _cell([rb[k] for k in keys], full)
    out = [f"# `{a}` vs `{b}`", "",
           f"The {len(keys)} trials both recorded (same task, chaos and seed). Code: "
           f"{', '.join(sorted({ra[k]['code'] for k in keys}))} → {', '.join(sorted({rb[k]['code'] for k in keys}))}.",
           "", f"| | `{a}` | `{b}` |", "|---|---|---|",
           f"| Passed the ground-truth grader | **{ca['passed']}/{ca['trials']}** ({_pct(ca['rate'])}) | "
           f"**{cb['passed']}/{cb['trials']}** ({_pct(cb['rate'])}) |",
           f"| Criteria met | {_pct(ca['criteria'])} | {_pct(cb['criteria'])} |",
           f"| Unsafe trials | {ca['unsafe']} | {cb['unsafe']} |",
           f"| Nova said \"verified\" and was wrong | {ca['verified_but_failed']} | {cb['verified_but_failed']} |",
           f"| Nova didn't claim a trial that passed | {ca['passed_unclaimed']} | {cb['passed_unclaimed']} |",
           f"| People interrupted per trial (mean) | {ca['interventions']} | {cb['interventions']} |",
           f"| Steps per trial (median) | {ca['steps']} | {cb['steps']} |",
           f"| Minutes per trial (median) | {ca['minutes']} | {cb['minutes']} |", "",
           "| Task | Chaos | `" + a + "` passed | `" + b + "` passed |", "|---|---|---|---|"]
    cells: dict[str, list[str]] = {}
    for k in keys:
        cells.setdefault(f"{rb[k]['task']}|{rb[k]['chaos']}", []).append(k)
    for cell, ks in sorted(cells.items()):
        task, chaos = cell.split("|")
        pa, pb = sum(ra[k]["passed"] for k in ks), sum(rb[k]["passed"] for k in ks)
        out.append(f"| {task.upper()} {TASKS[task].request} | {chaos} | {pa}/{len(ks)} | {pb}/{len(ks)} |")
    out += ["", "Trial by trial (✓ passed, ✗ failed; ⚠ unsafe; ! Nova said \"verified\" and was wrong):", "",
            "| Trial | `" + a + "` | `" + b + "` |", "|---|---|---|"]
    mark = lambda r: ("✓" if r["passed"] else "✗") + (" ⚠" if r["violations"] else "") + \
        (" !" if r["status"] == "verified" and not r["passed"] else "") + f" {r['status']}"  # noqa: E731
    out += [f"| {k} | {mark(ra[k])} | {mark(rb[k])} |" for k in keys]
    missing = sorted(set(ra) - set(rb))
    if missing:
        out += ["", f"Not yet in `{b}`: {', '.join(missing)}."]
    return "\n".join(out) + "\n"


def write(a: str, b: str) -> str:
    md = compare(a, b)
    (BENCH_DIR / f"compare-{a}-{b}.md").write_text(md, encoding="utf-8")
    return md
