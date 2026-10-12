"""The benchmark (run from the repo root; Ollama running; the world's servers are started if they aren't up).

    uv run python -m bench run [--tasks t1,t2,t3,t4] [--chaos off,heavy] [--trials 3]   the matrix
    uv run python -m bench memory [--task t3] [--pairs 2]        one task twice on one memory: does run 2 ask less?
    uv run python -m bench rpa [--chaos off,light,heavy] [--trials 3]   the scripted bot on T1, for comparison
    uv run python -m bench all                                   rpa, memory, then the matrix (an overnight run)
    uv run python -m bench summary                               rewrite summary.md / summary.json
    uv run python -m bench compare r1 r2                         two benchmarks side by side (var/bench/compare-r1-r2.md)

Everything goes to var/bench/<name>/ (--name, default "main"): results.jsonl (one line per trial), logs/, runs/
(each trial's own run store: `NOVA_RUNS_DIR=var/bench/main/runs/<key> uv run python -m agent show last`), and the
summary. Running the same command again skips the trials already recorded, so a stopped benchmark resumes.
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from bench import summary
from bench.harness import DEFAULT_TASKS, ROOT, TASKS, Aborted, Bench, run_matrix, run_memory, run_rpa, servers


def _list(text: str, allowed) -> list[str]:
    items = [x.strip() for x in text.split(",") if x.strip()]
    bad = [x for x in items if x not in allowed]
    if bad:
        raise SystemExit(f"unknown: {', '.join(bad)} (choose from {', '.join(allowed)})")
    return items


def main() -> int:
    load_dotenv(ROOT / ".env")
    from world.config import CHAOS_PROFILES

    p = argparse.ArgumentParser(prog="python -m bench", description=__doc__.split("\n\n")[0])
    p.add_argument("--name", default="main", help="benchmark folder under var/bench (default: main)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="tasks × chaos × trials")
    r.add_argument("--tasks", default=",".join(DEFAULT_TASKS))
    r.add_argument("--chaos", default="off,heavy")
    r.add_argument("--trials", type=int, default=3)
    m = sub.add_parser("memory", help="one task twice on one memory")
    m.add_argument("--task", default="t3", choices=list(TASKS))
    m.add_argument("--pairs", type=int, default=2)
    b = sub.add_parser("rpa", help="the scripted bot on T1")
    b.add_argument("--chaos", default="off,light,heavy")
    b.add_argument("--trials", type=int, default=3)
    a = sub.add_parser("all", help="rpa, memory, then the matrix, with the defaults above")
    a.add_argument("--trials", type=int, default=3)
    sub.add_parser("summary", help="rewrite the summary from results.jsonl")
    c = sub.add_parser("compare", help="two benchmarks side by side (before / after)")
    c.add_argument("before")
    c.add_argument("after")
    args = p.parse_args()

    if args.cmd == "compare":
        from bench.compare import write

        print(write(args.before, args.after))
        return 0
    bench = Bench(args.name)
    say = lambda s: print(s, flush=True)  # noqa: E731
    try:
        if args.cmd != "summary":
            with servers(say):
                if args.cmd in ("rpa", "all"):
                    chaos = _list(args.chaos, CHAOS_PROFILES) if args.cmd == "rpa" else ["off", "light", "heavy"]
                    run_rpa(bench, chaos, args.trials, say)
                if args.cmd in ("memory", "all"):
                    run_memory(bench, args.task if args.cmd == "memory" else "t3",
                               args.pairs if args.cmd == "memory" else 2, say)
                if args.cmd in ("run", "all"):
                    tasks = _list(args.tasks, TASKS) if args.cmd == "run" else DEFAULT_TASKS
                    chaos = _list(args.chaos, CHAOS_PROFILES) if args.cmd == "run" else ["off", "heavy"]
                    run_matrix(bench, tasks, chaos, args.trials, say)
    except Aborted as e:
        say(f"Benchmark stopped: {e}")
        return 2
    except KeyboardInterrupt:
        say("Benchmark interrupted; run the same command again to carry on.")
        return 130
    finally:
        if bench.rows():
            s = summary.write(bench)
            o = s["overall"]
            if o:
                say(f"\n{o['passed']}/{o['trials']} trials passed; unsafe: {o['unsafe']}; "
                    f"wrongly 'verified': {o['verified_but_failed']}")
            say(f"Summary: {(bench.dir / 'summary.md').relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
