"""World control: `python -m world <command>`.

  reset [scenario] [--chaos off|light|heavy] [--seed N]   rebuild the company from scratch
  serve                                                  run the finance portal and mail server
  chaos <profile>                                        change fault injection live
  check [scenario] [--facts F]                           grade the current state against ground truth
  info                                                   URLs, demo logins, scenario list
"""

from __future__ import annotations

import argparse
import asyncio
import json

from world import config


def _serve() -> None:
    import uvicorn

    async def main() -> None:
        servers = [uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
                   for app, port in (("world.portal.app:app", config.PORTAL_PORT),
                                     ("world.mail.app:app", config.MAIL_PORT))]
        print(f"Finance portal  {config.PORTAL_URL}\nMail server     {config.MAIL_URL}  (API docs at /docs)")
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(main())


def main() -> None:
    from world.scenarios import SCENARIOS

    parser = argparse.ArgumentParser(prog="python -m world")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reset")
    r.add_argument("scenario", nargs="?", default="demo", choices=list(SCENARIOS))
    r.add_argument("--chaos", default="off", choices=list(config.CHAOS_PROFILES))
    r.add_argument("--seed", type=int, default=7)
    sub.add_parser("serve")
    ch = sub.add_parser("chaos")
    ch.add_argument("profile", choices=list(config.CHAOS_PROFILES))
    c = sub.add_parser("check")
    c.add_argument("scenario", nargs="?", default=None, choices=list(SCENARIOS))
    c.add_argument("--json", action="store_true")
    c.add_argument("--facts", help="the agent runtime's record of approvals and questions (facts.json), so "
                                   "approval-bound criteria are evaluated instead of reported as n/a")
    sub.add_parser("info")
    args = parser.parse_args()

    if args.cmd == "reset":
        from world.scenarios import reset
        print(json.dumps(reset(args.scenario, chaos=args.chaos, seed=args.seed)))
    elif args.cmd == "serve":
        _serve()
    elif args.cmd == "chaos":
        state = config.load_state()
        state["chaos"] = args.profile
        config.save_state(state)
        print(f"chaos → {args.profile}")
    elif args.cmd == "check":
        from pathlib import Path

        from world.checkers import RunFacts, check
        facts = None
        if args.facts:
            raw = json.loads(Path(args.facts).read_text(encoding="utf-8"))
            facts = RunFacts(approvals=raw.get("approvals", []), questions=raw.get("questions", []))
        report = check(args.scenario, facts)
        print(json.dumps(report.as_dict(), indent=2) if args.json else report.render())
    elif args.cmd == "info":
        state = config.load_state()
        print(f"Scenario: {state['scenario']}   chaos: {state['chaos']}   business date: {state['as_of']}")
        print(f"Finance portal: {config.PORTAL_URL}  (humans: rahul.mehta / northwind, priya.nair / northwind)")
        print(f"Webmail:        {config.MAIL_URL}    Mail API docs: {config.MAIL_URL}/docs")
        print(f"Drive:          {config.DRIVE}")
        print("Scenarios:")
        for name, desc in SCENARIOS.items():
            print(f"  {name:22} {desc}")


if __name__ == "__main__":
    main()
