"""The runtime: deterministic code around one model-driven loop.

    UNDERSTAND  the model picks the handbook procedures and reads the documents the request is about
    CONTRACT    one focused call writes the goal, the facts (checked against the documents), the success
                criteria and the plan (the procedure's own steps), before anything is changed
    ACT         plan ⇄ act ⇄ observe, one tool call at a time, in a fresh context that starts from the contract.
                Every consequential action passes provenance (values must have been seen), the maker-checker
                (dates and amounts worked out again) and the policy gate (agent/policy.py)
    VERIFY      at finish, an independent read-only check of the systems against the contract (agent/verify.py),
                with one chance to fix what it finds
    REPORT      report.md, evidence.json and facts.json (agent/report.py)

A person can be needed at any point (a question, an approval). If they don't answer straight away the run pauses:
its whole state is saved, the process can exit, and `python -m agent resume <run>` carries on where it stopped.

The runtime holds what a small model can't: the contract and its facts, the plan's progress, approvals, answers,
budgets, and a bounded view of the history (only the latest results in full; older ones shrink to one line).
"""

from __future__ import annotations

import json
import re
import time
import traceback
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime

import httpx

from agent import contract, prompts, provenance, report, review
from agent.config import CLOCKS, Settings
from agent.eventlog import EventLog
from agent.human import Pause
from agent.llm import LLMError, ToolCall
from agent.memory import Handbook, Lessons, Section
from agent.policy import Policy
from agent.result import Result
from agent.state import RunState, Step
from agent.tools import Ctx, MailClient, Registry, ToolError, build_registry, calc, date_calc, plan_text
from agent.verify import Verifier

CHARS_PER_TOKEN = 2.7  # Qwen's tokenizer on this mix of prose, IDs and digits (measured against Ollama's counts)
UNDERSTAND_NUDGE = 8  # model calls spent understanding before the runtime asks for the plan
UNDERSTAND_LIMIT = 12  # ... and before it writes the contract from what has been read so far
PROGRESS_NUDGE = 6  # model calls without marking a plan step before the runtime asks for step_done
REPEAT_REFUSE = 2  # an identical call already made this many times is refused (with its result) instead of run
REPEAT_STOP = 5  # ... and at this many in the recent window the run stops as stuck
SYSTEM_FAILS = 2  # the same call failing this often with a system error: stop (blocked) rather than guess
SUBMITS = ("browser_fill", "sheet_append_row", "sheet_write", "mail_send", "mail_forward")  # what puts work on record
VERIFY_ROUNDS = 1  # chances Nova gets to fix what the independent check found
SYSTEM_ERROR = re.compile(r"mail server said 5\d\d|not reachable|Service Unavailable|ERR_APP_BUSY|ERR_HTTP_5", re.I)
END_STATUS = {"verified": "verified", "failed": "verification_failed", "unverified": "finished"}
STALLS = ("stuck", "out_of_steps", "out_of_time")  # the model stopped making progress (not a system failure)
OBJECTION = "an independent check that worked the values out again"  # the start of the checker's objection


class PreflightError(RuntimeError):
    """Nova can't safely start: a system it needs is unreachable or doesn't tell it what it needs."""


def _wsl_hint() -> str:
    try:
        if "microsoft" in open("/proc/version").read().lower():
            return (" (This is running inside WSL, where 127.0.0.1 is the Linux VM, not Windows: run Nova from a "
                    "Windows PowerShell terminal instead.)")
    except OSError:
        pass
    return ""


def _key(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _canon(v) -> str:
    return provenance.as_date(v) or provenance.as_number(v) or str(v).strip().lower()


@dataclass
class Loop:
    """Everything the loop needs to carry on, including after a restart: it is saved whenever the run pauses."""
    system: str = ""
    msgs: list[dict] = field(default_factory=list)
    calls: int = 0
    no_tool: int = 0
    fails: int = 0
    recent: list[str] = field(default_factory=list)  # the latest calls (tool + arguments)
    last_result: dict = field(default_factory=dict)  # call → summary of its latest result
    stamps: dict = field(default_factory=dict)  # call → what its outcome depended on, after its latest attempt
    failures: dict = field(default_factory=dict)  # call → times it failed because a system wasn't working
    notes: list[str] = field(default_factory=list)  # runtime warnings for the next status block
    last_progress: int = 0
    elapsed: float = 0.0  # seconds spent in earlier sessions of this run
    pending: list[dict] = field(default_factory=list)  # tool calls of the latest reply that haven't run yet
    skip_rest: bool = False  # a write in this reply failed: the rest of the reply is not run
    contract_done: bool = False
    questions: list[str] = field(default_factory=list)  # the contract's questions for the requester
    page_section: str = ""  # the handbook section governing the form on screen ("doc|heading")
    page_text: str = ""
    page_url: str = ""
    page_version: int = 0  # +1 whenever a page is (re)opened or a click changes it: a form filled again is a new try
    open_problem: str = ""  # the latest submission (form, row, email) that failed and hasn't been put right since
    objected: list[str] = field(default_factory=list)  # "field|value" the checker objected to (resubmit overrides)
    verify_rounds: int = 0
    verification: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)  # tool calls inside messages become plain dicts

    @classmethod
    def from_dict(cls, d: dict) -> "Loop":
        loop = cls(**d)
        for m in loop.msgs:
            if m.get("tool_calls"):
                m["tool_calls"] = [c if isinstance(c, ToolCall) else ToolCall(**c) for c in m["tool_calls"]]
        return loop


class Runtime:
    def __init__(self, settings: Settings, llm, human, log: EventLog, policy: Policy | None = None):
        self.s, self.llm, self.human, self.log = settings, llm, human, log
        self.policy = policy or Policy.load()
        self.num_ctx = getattr(llm, "num_ctx", 32768)
        self.model = getattr(llm, "model", "?")
        self.evidence = provenance.Evidence()
        self._t0 = time.time()

    # ------------------------------------------------------------------ public
    def run(self, request: str, requester: str, parent: str = "") -> RunState:
        vault = self.s.load_vault()
        run_id = self.log.new_run(request, requester, self.model)
        ctx = self._ctx(RunState(run_id, request, requester, today="", parent=parent), vault)
        ctx.log("run.start", request=request, requester=requester, model=self.model, parent=parent,
                settings={"max_steps": self.s.max_steps, "keep_full": self.s.keep_full, "think": self.s.think,
                          "num_ctx": self.num_ctx})
        st = self._drive(ctx, vault, Loop(), fresh=True)
        return self._run_items(st) if self.log.run(run_id)["status"] == "split" else st

    def _run_items(self, parent: RunState) -> RunState:
        """A split request: each item is its own run (own contract, plan, verification), one after another. A
        waiting item pauses the whole request; resuming it carries on with the rest (see resume)."""
        # one request, one business date: an item doesn't read it again (seen 2026-10-09, heavy chaos: an item's
        # sign-in for the date failed once, the item didn't start, and the request stopped with two invoices undone)
        settings = replace(self.s, today=self.s.today or parent.today)
        for n, item in enumerate(parent.work_items, 1):
            if item["status"] not in ("todo", "running"):
                continue
            child = Runtime(settings, self.llm, self.human, self.log, self.policy).run(
                item["request"], parent.requester, parent=parent.run_id)
            item["run_id"], item["status"] = child.run_id, self.log.run(child.run_id)["status"]
            self.log.append(parent.run_id, "work.item", n=n, request=item["request"], run_id=child.run_id,
                            status=item["status"])
            self.log.update_run(parent.run_id, state=parent.to_dict())
            if item["status"] == "waiting":
                self.log.update_run(parent.run_id, status="waiting", state=parent.to_dict())
                return parent
            if item["status"] in ("not_started", "model_error", "interrupted"):  # the systems or the model are down
                break
        return self._close_split(parent)

    def _close_split(self, parent: RunState) -> RunState:
        """The record of a split request: its items' results, approvals and questions together."""
        statuses = [i["status"] for i in parent.work_items]
        status = ("verified" if all(s == "verified" for s in statuses)
                  else "finished" if all(s in ("verified", "finished") for s in statuses) else "incomplete")
        facts = {"approvals": [], "questions": []}
        for item in parent.work_items:
            path = self.log.run_dir(item["run_id"]) / "facts.json" if item["run_id"] else None
            if path and path.exists():
                child = json.loads(path.read_text(encoding="utf-8"))
                facts["approvals"] += child.get("approvals", [])
                facts["questions"] += child.get("questions", [])
        parent.summary = "; ".join(f"{n}. {i['request']}: {i['status']}" for n, i in enumerate(parent.work_items, 1))
        self.log.append(parent.run_id, "run.end", status=status, summary=parent.summary, verdict=None)
        self.log.update_run(parent.run_id, state=parent.to_dict(), snapshot=None)
        self.log.end_run(parent.run_id, status, parent.summary)
        run_dir = self.log.run_dir(parent.run_id)
        md, evidence = report.build(parent, status, self.log.events(parent.run_id), None, self.model)
        (run_dir / "facts.json").write_text(json.dumps(facts, indent=2), encoding="utf-8")
        (run_dir / "evidence.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False, default=str),
                                               encoding="utf-8")
        (run_dir / "report.md").write_text(md, encoding="utf-8")
        return parent

    def resume(self, run_id: str) -> RunState:
        """Carry on a run that paused for a person, from exactly where it stopped (after a restart too). For a split
        request, that is its waiting item; once that is done, the remaining items follow."""
        row = self.log.run(run_id)
        if not row:
            raise ValueError(f"no run {run_id!r}")
        state = json.loads(row["state"]) if row.get("state") else {}
        waiting_item = next((i["run_id"] for i in state.get("work_items", []) if i["status"] == "waiting"), None)
        if row["status"] == "waiting" and waiting_item:
            return self.resume(waiting_item)
        if row["status"] != "waiting" or not row.get("snapshot"):
            raise ValueError(f"run {row['id']} is {row['status']}; only a waiting run can be resumed")
        snap = json.loads(row["snapshot"])
        vault = self.s.load_vault()
        st = RunState.from_dict(snap["state"])
        ctx = self._ctx(st, vault)
        ctx.today = datetime.strptime(st.today, "%d-%m-%Y").date()
        ctx.created_files, ctx.sent = set(snap["ctx"]["created_files"]), snap["ctx"]["sent"]
        ctx.contacts = set(snap["ctx"]["contacts"])
        ctx.vendors = list(snap["ctx"].get("vendors", []))
        ctx.rows = list(snap["ctx"].get("rows", []))
        ctx.done = dict(snap["ctx"].get("done", {}))
        ctx.uncertain = set(snap["ctx"].get("uncertain", []))
        self.evidence = provenance.Evidence.load(snap["evidence"])
        self.log.update_run(st.run_id, status="running")
        ctx.log("run.resume", waiting_for=st.waiting_for)
        st = self._drive(ctx, vault, Loop.from_dict(snap["loop"]), fresh=False)
        return self._continue_parent(st) if st.parent and self.log.run(st.run_id)["status"] != "waiting" else st

    def _continue_parent(self, child: RunState) -> RunState:
        """An item of a split request finished after a pause: record it, and carry on with the remaining items."""
        status = self.log.run(child.run_id)["status"]
        parent = RunState.from_dict(json.loads(self.log.run(child.parent)["state"]))
        for item in parent.work_items:
            if item["run_id"] == child.run_id:
                item["status"] = status
                self.log.append(parent.run_id, "work.item", request=item["request"], run_id=child.run_id,
                                status=status)
        self.log.update_run(parent.run_id, status="running", state=parent.to_dict())
        return self._run_items(parent)

    # ------------------------------------------------------------------ setup
    def _ctx(self, st: RunState, vault: dict) -> Ctx:
        handbook = self._handbook = Handbook(self.s.drive / "Handbook")
        mail = vault.get("mail")
        emit = lambda kind, /, **data: self.log.append(st.run_id, kind, **data)  # noqa: E731
        return Ctx(state=st, drive=self.s.drive, run_dir=self.log.run_dir(st.run_id), today=date.today(),
                   handbook=handbook, human=self.human,
                   mail=MailClient(mail["url"], mail["api_key"], mail["mailbox"]) if mail else None,
                   sites={k: v for k, v in vault.items() if v.get("password")}, log=emit, headless=self.s.headless,
                   policy=self.policy, lessons=Lessons(self.log.db))

    def _drive(self, ctx: Ctx, vault: dict, loop: Loop, fresh: bool) -> RunState:
        status = "failed"
        self._t0 = time.time() - loop.elapsed
        try:
            problems = self._preflight(vault)
            if problems:
                for p in problems:
                    ctx.log("error", where="preflight", message=p)
                status = "not_started" if fresh else "waiting"
                return ctx.state
            if fresh:
                ctx.today = self._business_date(ctx)
                ctx.state.today = f"{ctx.today:%d-%m-%Y}"
                self._start(ctx, vault, loop)
            elif loop.page_url:  # put the screen back where it was when the run paused
                ctx.browser().open(loop.page_url)
            status = self._loop(ctx, build_registry(self.policy), loop)
        except Pause as p:
            ctx.log("run.pause", item=p.item_id, waiting_for=p.what)
            status = "waiting"
        except KeyboardInterrupt:
            status = "interrupted"
        except PreflightError as e:
            ctx.log("error", where="preflight", message=str(e))
            status = "not_started"
        except LLMError as e:
            ctx.log("error", where="model", message=str(e))
            status = "model_error"
        except Exception as e:  # a crash still leaves a complete record behind
            ctx.log("error", where="runtime", message=f"{e.__class__.__name__}: {e}", traceback=traceback.format_exc())
            status = "crashed"
        finally:
            ctx.close()
            self._close_run(ctx, loop, status)
        return ctx.state

    def _preflight(self, vault: dict) -> list[str]:
        """Before anything starts: can Nova reach its model and every company system? A run that starts without
        them only fails later, halfway, and less clearly."""
        problems = []
        ping = getattr(self.llm, "ping", None)
        if ping:
            problem = ping()
            if problem:
                problems.append(problem + _wsl_hint())
        for name, system in vault.items():
            url = system.get("url")
            if not url:
                continue
            try:
                httpx.get(url, timeout=5, follow_redirects=False)
            except httpx.TransportError as e:
                problems.append(f"the {name} system at {url} is not reachable ({e.__class__.__name__}). Is "
                                f"`uv run python -m world serve` running?" + _wsl_hint())
        return problems

    def _business_date(self, ctx: Ctx) -> date:
        """Today's date as the company's systems see it (the mock world's clock is frozen). If a system is supposed to
        tell us and can't, the run stops: guessing from the computer's clock would give the wrong date."""
        if self.s.today:
            d = datetime.strptime(self.s.today, "%d-%m-%Y").date()
            ctx.log("clock", source="settings", today=f"{d:%d-%m-%Y}")
            return d
        for site, pattern in CLOCKS.items():
            if site in ctx.sites:
                for attempt in range(2):  # a sign-in that fails once (an expired session, a busy server) is retried
                    page = ctx.browser().open(ctx.sites[site]["url"])
                    m = re.search(pattern, page.text)
                    if m:
                        break
                    time.sleep(2)
                if not m:
                    raise PreflightError(f"could not read the business date from the {site} "
                                         f"({page.summary[:200]}). Set NOVA_TODAY=DD-MM-YYYY to give it explicitly.")
                ctx.log("clock", source=site, today=m[1])
                return datetime.strptime(m[1], "%d-%m-%Y").date()
        ctx.log("clock", source="system", today=f"{date.today():%d-%m-%Y}")  # no system keeps a business date
        return date.today()

    def _choose_docs(self, ctx: Ctx) -> list[str]:
        """UNDERSTAND, part 1: the model picks the procedures from the handbook's contents (semantic, not keyword,
        retrieval); keyword search is the fallback."""
        st, hb = ctx.state, ctx.handbook
        chosen: list[str] = []
        try:
            reply = self.llm.chat([
                {"role": "system", "content": prompts.CHOOSE_DOCS_SYSTEM},
                {"role": "user", "content": prompts.CHOOSE_DOCS.format(requester=st.requester, request=st.request,
                                                                       toc=hb.toc())}],
                schema=prompts.CHOOSE_DOCS_SCHEMA, think=False)
            names = json.loads(reply.content or "{}").get("documents", [])
            for n in names:
                doc = hb.find(str(n))
                if doc and doc not in chosen:
                    chosen.append(doc)
            ctx.log("understand.docs", asked=names, chosen=chosen, seconds=round(reply.seconds, 1))
        except (ValueError, LLMError) as e:
            ctx.log("runtime.warning", message=f"document choice failed: {e}")
        if not chosen:
            best, _ = hb.retrieve(st.request)
            chosen = [best] if best else []
            ctx.log("understand.docs", asked=[], chosen=chosen, fallback="bm25")
        # follow the handbook's own references ("refund to Northwind's bank account (see the Company Overview)"): the
        # referenced document goes right after the one citing it (seen 2026-10-08: a refund request without the
        # account to refund to, because the Company Overview was never read)
        for doc in list(chosen):
            for ref in re.findall(r"\bsee (?:the )?([A-Z][\w&]*(?: [A-Z][\w&]*){0,3})", hb.docs[doc]):
                target = next((d for d, t in hb.titles.items() if ref.lower() in t.lower()), None)
                if target and target not in chosen:
                    chosen.insert(chosen.index(doc) + 1, target)
                    ctx.log("understand.reference", document=doc, reference=ref, added=target)
        budget, out = int(self.num_ctx * 0.25 * CHARS_PER_TOKEN), []  # handbook text ≤ a quarter of the window
        for doc in chosen[:5]:
            if sum(len(hb.docs[d]) for d in out) + len(hb.docs[doc]) <= budget or not out:
                out.append(doc)
        return out

    def _start(self, ctx: Ctx, vault: dict, loop: Loop) -> None:
        st, hb = ctx.state, ctx.handbook
        st.handbook_docs = self._choose_docs(ctx)
        loop.system = prompts.SYSTEM.format(
            portal_url=vault.get("portal", {}).get("url", "(not configured)"),
            mailbox=vault.get("mail", {}).get("mailbox", "?"), toc=hb.toc(), today=st.today, weekday=f"{ctx.today:%A}")
        text = "\n\n".join(f"=== Handbook: {d} ({hb.titles[d]}) ===\n{hb.docs[d].strip()}" for d in st.handbook_docs) \
            or "(No handbook document was selected; use handbook_search.)"
        first = prompts.FIRST.format(requester=st.requester, request=st.request, handbook=text,
                                     split=prompts.ITEM if st.parent else prompts.SPLIT)  # an item is not split again
        loop.msgs = [{"role": "system", "content": loop.system}, {"role": "user", "content": first}]
        self.evidence = provenance.Evidence()  # everything this task has observed, for the provenance check
        # A person's request is evidence. An item of a split request is not: the model wrote it while skimming several
        # documents (seen 2026-10-08: Hooli's amount and PO in the Sirius item), so its values must come from the
        # item's own document
        for t in ((st.today, text) if st.parent else (st.request, st.today, first)):
            self.evidence.add(t)
        # the handbook is the company's directory: its people's addresses are known, whichever documents a task reads
        self.evidence.emails |= {e.lower() for e in provenance.EMAIL.findall("\n".join(hb.docs.values()))}

    # ------------------------------------------------------------------ the loop
    def _loop(self, ctx: Ctx, reg: Registry, loop: Loop) -> str:
        st = ctx.state
        while True:
            stop = self._next_reply(ctx, reg, loop) if not loop.pending else None
            stop = stop or self._batch(ctx, reg, loop)
            if stop:
                if st.correcting and stop != "blocked":
                    return self._final_check(ctx, loop, stop)
                if stop in STALLS and st.phase == "act" and st.success_criteria:
                    return self._after_stall(ctx, loop, stop)
                return stop
            if st.finished:
                done = self._verify(ctx, loop)
                if done:
                    return done
            self._after_turn(ctx, loop)

    def _next_reply(self, ctx: Ctx, reg: Registry, loop: Loop) -> str | None:
        """One model call; its tool calls become the pending batch. Returns a status if the run must stop."""
        st = ctx.state
        if loop.calls >= self.s.max_steps:
            ctx.log("runtime.stop", reason=f"step budget ({self.s.max_steps} model calls) used up")
            return "out_of_steps"
        if time.time() - self._t0 > self.s.max_minutes * 60:
            ctx.log("runtime.stop", reason=f"time budget ({self.s.max_minutes} min) used up")
            return "out_of_time"
        if st.phase == "understand" and loop.calls >= UNDERSTAND_LIMIT:  # write the contract from what was read
            ctx.log("runtime.warning", message=f"{loop.calls} calls spent understanding; writing the contract now")
            res = self._contract(ctx, loop, {})
            if st.work_items and not st.parent:
                return "split"
            if st.phase != "act":
                ctx.log("runtime.stop", reason=f"no plan could be written: {res.summary}")
                return "no_plan"
            self._enter_act(ctx, loop)
            return None
        think = self.s.think == "always" or (self.s.think == "recovery" and loop.fails >= 2)
        prompt, k = self._render(loop.msgs, st, loop.notes, reg)
        loop.notes = []
        reply = self.llm.chat(prompt, tools=reg.schemas(st.phase), think=think)
        loop.calls += 1
        calls = [{"name": c.name, "arguments": c.arguments} for c in reply.tool_calls]
        seq = ctx.log("llm.reply", n=loop.calls, phase=st.phase, think=think, content=reply.content,
                      thinking=reply.thinking[:4000], tool_calls=calls, prompt_tokens=reply.prompt_tokens,
                      output_tokens=reply.output_tokens, seconds=round(reply.seconds, 2),
                      est_prompt_tokens=self._estimate(prompt, reg, st), keep_full=k)
        self.log.llm_call(st.run_id, seq, {"messages": prompt, "think": think, "phase": st.phase},
                          {"content": reply.content, "thinking": reply.thinking, "tool_calls": calls})
        if not reply.tool_calls:
            loop.no_tool += 1
            loop.msgs.append({"role": "assistant", "content": (reply.content or "")[:600]})
            if loop.no_tool >= 3:
                ctx.log("runtime.stop", reason="three replies in a row without an action")
                return "no_action"
            loop.msgs.append({"role": "user", "content": prompts.NO_TOOL})
            return None
        loop.no_tool = 0
        said = self._said_done(reply.content or "", st, calls)
        if said:
            loop.notes.append(said)
        content = reply.content or ""
        if self._echo(content, loop.msgs):  # kept out of its own view, so the loop has nothing to feed on
            loop.notes.append("You just wrote, word for word, what you wrote before: that line of thought is not "
                              "getting anywhere. Read the latest result again and take the next step from it.")
            content = ""
        loop.msgs.append({"role": "assistant", "content": content, "tool_calls": reply.tool_calls})
        loop.pending, loop.skip_rest = [asdict(c) for c in reply.tool_calls], False
        return None

    def _derivation_hint(self, unsupported: list[str]) -> str:
        """A small model often works a value out in its head (GST = CGST + SGST) and then can't see why it is refused
        (seen 2026-10-08: ten tries, then a question to a person). If two numbers the task has seen add up to it, or
        differ by it, name the calc that makes it legitimate. The model still has to make the call."""
        seen = {float(n) for n in self.evidence.numbers if re.fullmatch(r"\d+(?:\.\d+)?", n)}
        terms = sorted(n for n in seen if n >= 100)
        show = lambda x: provenance.as_number(f"{x:.2f}") or f"{x:g}"  # noqa: E731
        hints = []
        for item in unsupported:
            value = item.rsplit("=", 1)[-1].strip()
            if provenance.as_date(value):  # a date is worked out from another date (seen 2026-10-08: a guessed due
                # date retried five times, never computed)
                hints.append(f"{value} is a date nothing has shown: if it counts from another date (an invoice date "
                             "plus payment terms, say), call date_calc(date=\"<that date>\", add_days=<days>) and use "
                             "its result.")
                continue
            raw = provenance.as_number(value)
            if raw is None:
                continue
            v = float(raw)
            found = next((f"{show(a)} + {show(v - a)}" for a in terms if a <= v - a and (v - a) in seen), None) or \
                next((f"{show(a)} - {show(a - v)}" for a in reversed(terms) if a - v >= 100 and (a - v) in seen), None)
            if found:
                hints.append(f"{show(v)} = {found}? If that is how you worked it out, call "
                             f"calc(expression=\"{found}\") first: its result counts as seen.")
        return (" " + " ".join(hints)) if hints else ""

    def _stamp(self, st: RunState, loop: Loop | None = None, name: str = "") -> str:
        """What a call's outcome can depend on: what the task has seen (provenance), the approvals, the plan; for a
        form fill, also the page (seen 2026-10-09: after a 503, the form was opened again, a pop-up dismissed and the
        same values filled in, which was stopped as a fifth identical try although the first two hit the error page)."""
        ev = self.evidence
        page = f"|p{loop.page_version}" if loop is not None and name == "browser_fill" else ""
        return (f"{len(ev.ids)}|{len(ev.dates)}|{len(ev.numbers)}|{len(st.approvals)}|"
                f"{sum(s.status != 'todo' for s in st.plan)}|{len(st.plan)}|{st.phase}{page}")

    @staticmethod
    def _settle_objections(loop: Loop, tool: str) -> None:
        """Once a submission of the same kind is accepted, an earlier objection from the checker is settled. Left in
        view, a wrong one keeps steering a small model (seen 2026-10-08: after a mistaken "TDS on the net payable"
        objection, the model kept re-explaining that phantom rule until it stalled)."""
        for m in loop.msgs:
            if m.get("role") == "tool" and m.get("tool_name") == tool and OBJECTION in str(m.get("content", "")):
                m["content"] = m["summary"] = ("(settled: an independent check objected here, and what was submitted "
                                               "after it was accepted)")

    @staticmethod
    def _echo(content: str, msgs: list[dict]) -> bool:
        """Is the model repeating one of its own earlier messages word for word? (A loop feeding on itself.)"""
        key = re.sub(r"\s+", " ", content).strip().lower()[:300]
        return len(key) > 40 and any(m.get("role") == "assistant" and
                                     re.sub(r"\s+", " ", m.get("content") or "").strip().lower()[:300] == key
                                     for m in msgs)

    @staticmethod
    def _form_hint(loop: Loop) -> str:
        """The model keeps reopening a page whose form is waiting to be filled (seen 2026-10-07: five opens of the
        payment page, never filling it): name the form and the call that fills it."""
        m = re.search(r'^FORM "([^"]+)"(?: — submit button: "([^"]+)")?\n((?:  \S.*\n?)+)', loop.page_text, re.M)
        if not m:
            return ""
        labels = [f.group(1) for f in re.finditer(r"^  (.+?)\*?: ", m.group(3), re.M)]
        if not labels:
            return ""
        path = "/" + loop.page_url.split("://", 1)[-1].split("/", 1)[-1] if "://" in loop.page_url else loop.page_url
        fields = ", ".join(f'"{lbl}": ...' for lbl in labels[:8])
        submit = f', submit="{m.group(2)}"' if m.group(2) else ""
        return (f" You are on {path}, and its form \"{m.group(1)}\" is waiting to be filled: call "
                f"browser_fill(fields={{{fields}}}{submit}).")

    @staticmethod
    def _said_done(content: str, st: RunState, calls: list[dict]) -> str:
        """A small model often *says* a step is complete without calling step_done; the plan then keeps showing that
        step as next while the model works ahead of it (seen 2026-10-07: five identical searches, then stuck)."""
        if any(c["name"] == "step_done" for c in calls):
            return ""
        for m in re.finditer(r"\bstep\s*(\d+)\s*(?:is\s+|has\s+been\s+)?(?:now\s+)?(?:complete|completed|done|"
                             r"finished)\b", content, re.I):
            if int(m[1]) in st.open_steps():
                return (f"You wrote that step {m[1]} is complete, but the plan still shows it open. Call step_done "
                        f"for step {m[1]} with what you found, so the plan moves on.")
        return ""

    def _batch(self, ctx: Ctx, reg: Registry, loop: Loop) -> str | None:
        """Run the pending tool calls in order. A call that waits for a person raises Pause and stays pending."""
        while loop.pending:
            call = ToolCall(**loop.pending[0])
            res, ran = self._execute(ctx, reg, loop, call)
            loop.pending.pop(0)
            if ran and res.ok and call.name in ("browser_fill", "request_approval"):
                self._settle_objections(loop, call.name)
            loop.msgs.append({"role": "tool", "tool_name": call.name, "tool_call_id": call.id, "content": res.text,
                              "summary": res.summary, "ok": res.ok})
            stop = self._account(ctx, reg, loop, call, res, ran)
            if stop:
                return stop
            if ctx.state.work_items and not ctx.state.parent:  # split: each item becomes its own run
                loop.pending = []
                for item in ctx.state.work_items:  # described by its document, whichever way the split came
                    item["request"] = self._item_request(item["request"], loop)
                return "split"
            if ctx.state.finished:
                loop.pending = []
            if loop.contract_done:
                for rest in loop.pending:
                    ctx.log("tool.skipped", name=rest["name"], reason="the task contract was just written")
                self._enter_act(ctx, loop)
        return None

    def _execute(self, ctx: Ctx, reg: Registry, loop: Loop, call: ToolCall) -> tuple[Result, bool]:
        """The guards, in order, then the tool. Returns (result, whether the tool actually ran)."""
        st = ctx.state
        if loop.skip_rest:
            return Result.error("not run, because an earlier action in the same reply failed. Look at that result "
                                "first."), False
        key = call.name + json.dumps(call.arguments, sort_keys=True, default=str)
        if not st.waiting_for:  # after a pause, the same call comes back: it was logged the first time
            ctx.log("tool.call", name=call.name, arguments=call.arguments)
        t0 = time.time()
        # a repeat counts only while nothing its outcome depends on has changed since the last try (seen 2026-10-08:
        # a fill blocked for an uncomputed GST was refused, then stopped as "stuck", right after the model computed
        # the GST). After a change, the same call is a fresh try: its count starts again
        if key in loop.recent and loop.stamps.get(key) != self._stamp(st, loop, call.name):
            loop.recent = [k for k in loop.recent if k != key]
        repeats = loop.recent.count(key)
        if repeats >= REPEAT_REFUSE:
            ctx.log("runtime.refused", name=call.name, times=repeats)
            # the concrete way forward first: a small model acts on the first sentence it can use
            hint = self._form_hint(loop).strip() if call.name in ("browser_open", "browser_click") else ""
            open_steps = "; ".join(f"{i}. {st.plan[i - 1].text[:70]}" for i in st.open_steps()[:3])
            last = loop.last_result.get(key, "(see above)")
            # what actually needs putting right (seen 2026-10-09: the vendor list reopened five times while the payment
            # form's refusal, naming the one empty field and its only option, went unanswered)
            problem = (f"Still unresolved: {loop.open_problem}. Put that right. "
                       if loop.open_problem and loop.open_problem not in last else "")
            res = Result.error(f"not done again: you already did exactly this {repeats} times, and the result was: "
                               f"{last}. Doing it again cannot change that. {problem}"
                               + (f"{hint} " if hint else "") + "Decide what it means. "
                               + (f"Steps still open: {open_steps}. If this result finishes one of them, call "
                                  "step_done for it now; otherwise do" if open_steps else "Do")
                               + " something different (other search words, another page, the handbook, or ask_human).")
            ctx.log("tool.result", name=call.name, ok=False, summary=res.summary, text=res.text, data={}, seconds=0)
            return res, False
        if st.phase == "act" and call.name in ("mail_send", "mail_forward") and isinstance(call.arguments, dict):
            self._fix_recipients(ctx, call)
        if st.phase == "act":
            unsupported = provenance.unsupported(call.name, call.arguments, self.evidence)
            if unsupported:
                ctx.log("provenance.blocked", name=call.name, values=unsupported)
                res = Result.error("these values do not appear in anything you have read, been told or calculated "
                                   f"in this task: {'; '.join(unsupported)}. Copy each one from the page or document "
                                   "where it appears, or compute it first with calc or date_calc. Nothing was changed."
                                   + self._derivation_hint(unsupported) + self._directory_hint(ctx, unsupported))
                ctx.log("tool.result", name=call.name, ok=False, summary=res.summary, text=res.text, data={}, seconds=0)
                return res, False
            objections = self._check(ctx, loop, call)
            if objections:
                res = Result.error(
                    "an independent check that worked the values out again from the handbook disagrees, so nothing "
                    "was submitted:\n" + "\n".join(f"- {p['field']}: you entered {p['value']}; worked out per the "
                                                   f"handbook: {p['checker']}" for p in objections)
                    + "\nHow the check worked them out:\n" + "\n".join(f"  {t}" for t in objections[0]["trace"])
                    + "\nCompare this with the handbook rule for each field, then submit the right values.")
                ctx.log("tool.result", name=call.name, ok=False, summary=res.summary, text=res.text, data={}, seconds=0)
                return res, False
        args = call.arguments if isinstance(call.arguments, dict) else {}
        remembered = self._from_memory(ctx, str(args.get("question") or "").strip()) \
            if call.name == "ask_human" else None
        if remembered is not None:  # a person's standing instruction answers it: they aren't interrupted again
            res = remembered
        elif call.name == "submit_plan" and st.phase == "understand":
            res = self._contract(ctx, loop, args)
        else:
            res = reg.call(ctx, call.name, call.arguments, st.phase)  # may raise Pause: the call stays pending
            if call.name.startswith("browser_"):
                res.text += self._guidance(ctx, loop, res)
                loop.page_text, loop.page_url = res.text, str(res.data.get("url") or loop.page_url)
        self.evidence.add(res.text)
        ctx.log("tool.result", name=call.name, ok=res.ok, summary=res.summary, text=res.text, data=res.data,
                seconds=round(time.time() - t0, 2))
        return res, True

    def _account(self, ctx: Ctx, reg: Registry, loop: Loop, call: ToolCall, res: Result, ran: bool) -> str | None:
        """Bookkeeping after each call: repeats, system failures, failure streaks. Returns a status to stop with."""
        key = call.name + json.dumps(call.arguments, sort_keys=True, default=str)
        repeats = loop.recent.count(key)
        loop.recent = (loop.recent + [key])[-8:]
        if ran or not res.text.startswith("ERROR: not done again"):  # a guard's refusal is a result too
            loop.last_result[key] = res.summary
        if ran and (call.name == "browser_open" or (call.name == "browser_click" and res.ok)):
            loop.page_version += 1  # opens and clicks still count as repeats themselves: their stamp has no page
        if ran and call.name in SUBMITS:
            loop.open_problem = "" if res.ok else res.summary[:300]
        loop.stamps[key] = self._stamp(ctx.state, loop, call.name)
        if repeats + 1 >= REPEAT_STOP:
            ctx.log("runtime.stop", reason=f"stuck: {call.name} repeated {repeats + 1} times")
            return "stuck"
        if ran and not res.ok and SYSTEM_ERROR.search(res.text):
            loop.failures[key] = loop.failures.get(key, 0) + 1
            if loop.failures[key] >= SYSTEM_FAILS:
                ctx.log("runtime.stop", reason=f"blocked: {call.name} keeps failing because a system is not working "
                                               f"({res.summary[:200]}). Nova stops rather than guess; ask an "
                                               "administrator to fix it, then run the task again.")
                return "blocked"
        if ran or not loop.skip_rest:  # refused or blocked calls are failures too; calls skipped after one aren't
            loop.fails = 0 if res.ok else loop.fails + 1
        tool = reg.get(call.name)
        if not res.ok and tool is not None and tool.kind == "write":
            loop.skip_rest = True
        if call.name == "step_done" and res.ok:
            # progress: the same call may now have another outcome (finish refused for an open step, then the step
            # is done), so the repeat window starts again
            loop.last_progress, loop.recent = loop.calls, []
        return None

    def _after_turn(self, ctx: Ctx, loop: Loop) -> None:
        st = ctx.state
        if loop.fails >= 3:
            loop.notes.append(f"{loop.fails} actions in a row have failed. Stop and check your assumptions: read the "
                              "current page or the handbook again, or ask_human.")
        if st.phase == "understand" and loop.calls >= UNDERSTAND_NUDGE:
            loop.notes.append("You have read enough to plan. Call submit_plan now (it also decides whether the "
                              "request covers several separate documents); you can still look things up "
                              "while carrying out the plan.")
        if st.phase == "act" and st.open_steps() and loop.calls - loop.last_progress >= PROGRESS_NUDGE:
            loop.notes.append("Keep the plan current: if you have finished the step shown as Next (or any other), "
                              "call step_done for it now with the IDs and values you found.")
            loop.last_progress = loop.calls
        for n in loop.notes:
            ctx.log("runtime.warning", message=n)
        self.log.beat(st.run_id)
        self.log.update_run(st.run_id, state=st.to_dict())

    # ------------------------------------------------------------------ verification
    def _verify(self, ctx: Ctx, loop: Loop) -> str | None:
        """VERIFY: fresh, read-only reads of the systems against the contract. Returns the final status, or None
        when Nova gets a chance to fix what was found."""
        st = ctx.state
        events = self.log.events(st.run_id)
        ctx.close()  # Nova's browser session ends; the verifier reads in a fresh one (it reopens if work goes on)
        verifier = Verifier(ctx.sites, ctx.mail, ctx.drive, ctx.run_dir, self.llm)
        t0 = time.time()
        try:
            result = verifier.run(st.success_criteria, events, self._record(st, events))
        finally:
            verifier.close()
        loop.verification = result
        ctx.log("verify", verdict=result["verdict"], counts=result["counts"], actions=result["actions"],
                criteria=result["criteria"], round=loop.verify_rounds, seconds=round(time.time() - t0, 1))
        if result["verdict"] == "failed" and loop.verify_rounds < VERIFY_ROUNDS:
            loop.verify_rounds += 1
            found = [f"{c['what']}: {c['detail']}" for c in result["actions"] if c["status"] == "fail"]
            found += [f"\"{c['criterion']}\": {c['detail']}" for c in result["criteria"] if c["status"] == "fail"]
            st.finished, st.phase, st.correcting = False, "act", True  # from now on, a change is a correction
            st.plan.append(Step("Look into what the independent check could not confirm, and fix only a real mistake: "
                                + "; ".join(found)[:700]))
            loop.notes.append("An independent read of the systems does not confirm everything (see the last plan "
                              "step). The check can be wrong too: it may have looked in the wrong place, or a value "
                              "changed because of a later step (a bill shows Paid once its payment is made). Open the "
                              "record and compare it with the handbook. Change something only if it is really wrong; "
                              "never change a correct record to match the check. Then mark the step done, saying "
                              "what you found, and call finish.")
            return None
        return END_STATUS[result["verdict"]]

    def _after_stall(self, ctx: Ctx, loop: Loop, stop: str) -> str:
        """The model stopped making progress, perhaps after the work was done (seen 2026-10-09: the last email sent,
        then date_calc of today, five times). The systems decide: the run counts as verified only if an independent
        read confirms every success criterion; otherwise it stays stopped. Read-only, so it can't make things worse."""
        ctx.log("runtime.warning", message=f"Nova stopped ({stop}): checking whether the systems show the work done")
        loop.verify_rounds = VERIFY_ROUNDS  # no fix round after a stall
        self._verify(ctx, loop)
        result = loop.verification or {}
        confirmed = result.get("verdict") == "verified" and all(c["status"] == "pass" for c in result.get("criteria", []))
        return "verified" if confirmed else stop

    def _final_check(self, ctx: Ctx, loop: Loop, stop: str) -> str:
        """The fix round after a failed verification stalled. The work may still be done (seen 2026-10-08: the missing
        email was sent, then the model kept re-reading it), so the systems decide, not the model's last words."""
        ctx.log("runtime.warning", message=f"the fix round ended ({stop}): checking the systems once more")
        loop.verify_rounds = VERIFY_ROUNDS  # this check is final: no further fix round
        return self._verify(ctx, loop) or stop

    @staticmethod
    def _record(st: RunState, events: list[dict]) -> str:
        """What the verifier's planner may use to find things in the systems: IDs, pages, emails, rows."""
        lines = ["Facts:"] + [f"- {f['name']}: {f['value']}" for f in st.facts]
        lines += ["Plan notes:"] + [f"- {s.text}: {s.result}" for s in st.plan if s.result]
        lines += ["Approvals:"] + [f"- {a.ref} ({a.kind}, {a.status}): {a.payload}" for a in st.approvals]
        call, post = {}, None
        records, mails, rows = [], [], []
        for e in events:
            k, d = e["kind"], e["data"]
            if k == "tool.call":
                call, post = d, None
            elif k == "browser.post" and d.get("action"):
                post = d
            elif k == "tool.result" and post and d["ok"]:
                url = str(d["data"].get("url", ""))
                records.append(f"- {post['action']}: page /{url.split('://', 1)[-1].split('/', 1)[-1].split('?')[0]}")
                post = None
            elif k == "mail.sent":
                mails.append(f"- email #{d['id']} to {', '.join(d['to'])}: \"{d['subject']}\"")
            elif k == "file.row_appended":
                rows.append(f"- {d['path']} row {d['row']}: " + ", ".join(f"{c}={v}" for c, v in d["values"].items()
                                                                        if v))
        return "\n".join(lines + ["Records created in the Finance Portal:"] + (records or ["- none"])
                         + ["Emails sent:"] + (mails or ["- none"]) + ["Spreadsheet rows added:"] + (rows or ["- none"]))

    # ------------------------------------------------------------------ guidance and the maker-checker
    @staticmethod
    def _section_for(ctx: Ctx, step: str) -> str:
        """The chosen procedures' section a plan step carries out: by title first, then by its words."""
        norm = lambda s: re.sub(r"[^a-z0-9 ]+", " ", re.sub(r"^step\s*\d+\s*[—–:-]*\s*", "", s.lower())).split()  # noqa: E731
        want = norm(step)
        for s in ctx.handbook.sections:
            title = norm(s.heading)
            if s.doc in ctx.state.handbook_docs and title and want[:len(title)] == title:
                return f"{s.doc}|{s.heading}"
        hits = [s for s, _ in ctx.handbook.search(step, k=8) if s.doc in ctx.state.handbook_docs]
        return f"{hits[0].doc}|{hits[0].heading}" if hits else ""

    def _section(self, key: str) -> Section | None:
        doc, _, heading = key.partition("|")
        return next((s for s in self._handbook.sections if s.doc == doc and s.heading == heading), None) if key else None

    def _fix_recipients(self, ctx: Ctx, call: ToolCall) -> None:
        """A recipient nobody mentioned that is plainly a misspelling of exactly one address the task has seen becomes
        that address, and the correction is logged. Seen 2026-10-09: the 4B model wrote "anya.iyer@" and "anyanya.iyer@"
        for ananya.iyer@ in four runs, and kept resending the same misspelling after the refusal named the right one.
        Only ever an address already seen; anything less certain is still refused by provenance."""
        for key in ("to", "cc"):
            value = call.arguments.get(key)
            items = value if isinstance(value, list) else [x for x in re.split(r"[,;\s]+", str(value or "")) if x]
            fixed = []
            for a in items:
                a = str(a).strip()
                near = "" if a.lower() in self.evidence.emails else self.evidence.closest(a)
                if near and provenance._likeness(a.lower(), near) >= 0.8:
                    ctx.log("recipient.corrected", given=a, used=near)
                    a = near
                fixed.append(a)
            if fixed and fixed != items:
                call.arguments[key] = fixed

    def _referenced(self, sec: Section, step: str) -> list[Section]:
        """The part of another handbook document a step's text points to ("refund … to Northwind's bank account (see
        the Company Overview)"), shown right under it (seen 2026-10-09: the Company Overview was in the prompt, far
        above, and four refund requests of five went out without the account number to refund to)."""
        out = []
        for m in re.finditer(r"\bsee (?:the )?([A-Z][\w&]*(?: [A-Z][\w&]*){0,3})", sec.text):
            target = next((d for d, t in self._handbook.titles.items() if m[1].lower() in t.lower()), None)
            if not target or target == sec.doc:
                continue
            # what the reference is for: the words just before it ("… to Northwind's bank account (see the …")
            about = " ".join(re.split(r"[.!?\n]\s", sec.text[:m.start()])[-1].split()[-8:])
            hits = [s for s, _ in self._handbook.search(about or step, k=30) if s.doc == target]
            if hits and hits[0] not in out:
                out.append(hits[0])
        return out

    def _sections(self, ctx: Ctx, query: str, k: int) -> list[Section]:
        hits = ctx.handbook.search(query, k=8)
        return [s for s, _ in ([(s, sc) for s, sc in hits if s.doc in ctx.state.handbook_docs] or hits)][:k]

    def _guidance(self, ctx: Ctx, loop: Loop, res: Result) -> str:
        """Just-in-time procedure: when a data-entry form is on screen, the handbook section that governs it is shown
        right under it (matched by the form's own words). A small model follows a rule it can see far more
        reliably than one it read 10,000 tokens ago."""
        loop.page_section = ""
        forms = [f for f in res.data.get("forms") or [] if f["role"] != "search" and not f["password"]
                 and len(f["fields"]) >= 3]
        if not forms:
            return ""
        query = " ".join([res.data.get("h1") or "", *(f["name"] for f in forms), *(l for f in forms for l in f["fields"])])
        found = self._sections(ctx, query, 1)
        if not found:
            return ""
        s = found[0]
        loop.page_section = f"{s.doc}|{s.heading}"
        ctx.log("guidance", form=forms[0]["name"], section=f"{s.doc} › {s.heading}")
        return (f"\n\n[handbook] What the procedure says for this form ({s.doc} › {s.heading or s.title}); follow "
                f"it for every field:\n{s.text}")

    def _check(self, ctx: Ctx, loop: Loop, call: ToolCall) -> list[dict] | None:
        """Maker-checker before a consequential commit: the dates and amounts are worked out again, independently,
        from the handbook section that governs them (agent/review.py). Returns the disagreements (None = go ahead).
        A value the checker objected to that the maker submits again unchanged is the maker's call: it goes
        through, and the override is logged."""
        args = call.arguments if isinstance(call.arguments, dict) else {}
        section = self._section(loop.page_section)
        if call.name == "browser_fill" and section is not None:
            # checked when typed, not only when submitted: fill-then-click must not skip the checker
            values, rules = args.get("fields") or {}, [section]
            action = f'submit the form on screen ("{args["submit"]}")' if args.get("submit") \
                else "fill in the form on screen"
        elif call.name == "request_approval" and isinstance(args.get("details"), dict):
            values = {**args["details"]}
            rules = self._sections(ctx, f"approval {args.get('action', '')} " + " ".join(values), 2)
            action = f"ask for approval to {str(args.get('action', 'act')).replace('_', ' ')}"
        else:
            return None
        if not isinstance(values, dict) or not rules:
            return None
        values = review.reviewable(values)
        insisted = {k: v for k, v in values.items() if f"{_key(k)}|{_canon(v)}" in loop.objected}
        if not values or len(insisted) == len(values):
            if insisted:
                ctx.log("review.override", name=call.name, fields=insisted)
            return None
        st = ctx.state
        facts = [f"- {f['name']}: {f['value']}" for f in st.facts]
        facts += [f"- step {i} ({s.text}): {s.result}" for i, s in enumerate(st.plan, 1) if s.result]
        facts += [f"- approval {a.ref} {a.status}: {a.payload}" for a in st.approvals]
        seen = [f"- {m['summary']}" for m in loop.msgs if m.get("role") == "tool"][-12:]
        rules_text = "\n\n".join(f"{s.doc} › {s.heading or s.title}:\n{s.text}" for s in rules)

        def tool(fn):
            def run(a: dict) -> str:
                try:
                    return fn(ctx, a).text
                except ToolError as e:
                    return f"ERROR: {e}"
            return run

        t0 = time.time()
        derived, trace = review.rederive(self.llm, {"calc": tool(calc), "date_calc": tool(date_calc)}, action,
                                         list(values), st.today, rules_text, "\n".join(facts) or "(none)",
                                         "\n".join(seen) or "(nothing yet)", loop.page_text[:3000])
        for t in trace:  # the derivations are observations: the maker may use their results
            self.evidence.add(t)
        problems = review.disagreements(values, derived, trace)
        overridden = {p["field"]: p["value"] for p in problems if f"{_key(p['field'])}|{_canon(p['value'])}"
                      in loop.objected}
        problems = [p for p in problems if p["field"] not in overridden]
        ctx.log("review", name=call.name, values=values, derived=derived, trace=trace, problems=problems,
                rules=[f"{s.doc} › {s.heading}" for s in rules], seconds=round(time.time() - t0, 1))
        if overridden:
            ctx.log("review.override", name=call.name, fields=overridden)
        for p in problems:
            loop.objected.append(f"{_key(p['field'])}|{_canon(p['value'])}")
            p["trace"] = trace
        return problems or None

    # ------------------------------------------------------------------ the contract
    def _contract(self, ctx: Ctx, loop: Loop, draft: dict) -> Result:
        """CONTRACT: one focused, schema-constrained call that sees the procedures and the full text of the
        documents, then a grounding check of every fact against those documents."""
        st, hb = ctx.state, ctx.handbook
        items = self._decide_split(ctx, loop)
        if items:
            st.work_items = [{"request": i, "run_id": "", "status": "todo"} for i in items]
            ctx.log("work.split", items=items, via="decision")
            return Result(f"The request covers {len(items)} separate documents; each becomes its own task:\n"
                          + "\n".join(f"{n}. {i}" for n, i in enumerate(items, 1)), f"Split into {len(items)} items")
        documents = contract.documents_from(loop.msgs)
        handbook = "\n\n".join(f"=== Handbook: {d} ({hb.titles[d]}) ===\n{hb.docs[d].strip()}" for d in st.handbook_docs)
        shown = {k: draft[k] for k in ("goal", "steps", "success_criteria", "questions") if draft.get(k)}
        prompt = [{"role": "system", "content": contract.SYSTEM},
                  {"role": "user", "content": contract.PROMPT.format(
                      requester=st.requester, request=st.request, today=st.today, handbook=handbook,
                      documents=documents, index=contract.index_from(loop.msgs),
                      draft=json.dumps(shown, ensure_ascii=False) if shown else "(none)",
                      work_items=contract.ONE_ITEM if st.parent else contract.WORK_ITEMS,
                      lessons=self._recall(ctx, loop, documents))}]
        data, seconds = None, 0.0
        # Measured on T1: without thinking the contract takes ~30 s and gets the facts right; with thinking it ran
        # for minutes and twice ended with no answer at all. So thinking only when asked for (--think always).
        for think in ((True, False) if self.s.think == "always" else (False, False)):
            try:
                reply = self.llm.chat(prompt, schema=contract.SCHEMA, think=think)
                seconds += reply.seconds
                data = json.loads(reply.content or "{}")
                ctx.log("contract.reply", think=think, seconds=round(reply.seconds, 1), output_tokens=reply.output_tokens,
                        thinking=reply.thinking[:6000], content=data)
                if contract.clean_steps(data.get("steps") or []) or self._items(st, data):
                    break
            except (ValueError, LLMError) as e:
                ctx.log("runtime.warning", message=f"contract attempt failed: {e}")
            data = None
        data = data or {"goal": draft.get("goal") or st.request, "facts": [],
                        "success_criteria": draft.get("success_criteria") or [], "steps": draft.get("steps") or [],
                        "questions": draft.get("questions") or []}
        items = list(dict.fromkeys(self._item_request(i, loop) for i in self._items(st, data)))
        if len(items) >= 2:  # several separate documents: each becomes its own task (seen 2026-10-08: one plan for
            # four invoices, 43 facts, and a small model lost among them)
            st.work_items = [{"request": i, "run_id": "", "status": "todo"} for i in items]
            ctx.log("work.split", items=items, via="contract")
            return Result(f"The request covers {len(items)} separate documents; each becomes its own task:\n"
                          + "\n".join(f"{n}. {i}" for n, i in enumerate(items, 1)), f"Split into {len(items)} items")
        steps = contract.clean_steps(data.get("steps") or [])[:20]
        if not steps:
            return Result.error("no plan could be written. Read the handbook procedure, then call submit_plan with "
                                "the goal and the steps.")
        # an item's description is the model's own skim, so it grounds nothing (see _start)
        sources = "\n".join([documents, handbook] + ([] if st.parent else [st.request]))
        claimed = [{"name": str(f.get("name", "")).strip(), "value": str(f["value"]).strip(),
                    "source": str(f.get("source", "")).strip(),
                    "grounded": contract.grounded(str(f["value"]), sources, ctx.today)}
                   for f in data.get("facts") or [] if isinstance(f, dict) and str(f.get("value", "")).strip()]
        facts = [f for f in claimed if f["grounded"]]  # a "fact" the documents don't contain is not shown again
        if len(facts) < len(claimed):
            ctx.log("contract.dropped_facts", facts=[f for f in claimed if not f["grounded"]])
        st.goal = str(data.get("goal") or draft.get("goal") or st.request).strip()
        # Values the documents don't have (a net amount worked out in the model's head, a made-up account number)
        # are blanked out of the criteria and the steps. A criterion is the definition of done, so one would make the
        # verifier fail correct work; a step is shown on every turn, so one would argue with every correct calc
        # (seen 2026-10-08: "net payable 1,48,000" in four steps, the model re-calculating 1,74,000 until stuck).
        known = provenance.Evidence()
        known.add(sources)
        people = provenance.Evidence()
        people.add("\n".join(ctx.handbook.docs.values()))
        roles = self._roles("\n".join(ctx.handbook.docs.values()))
        steps, masked_steps = self._mask(steps, known, people)
        st.success_criteria, masked = self._mask([str(c).strip() for c in data.get("success_criteria") or []], known,
                                                 people, roles)
        st.facts, st.plan, st.phase = facts, [Step(s, guide=self._section_for(ctx, s)) for s in steps], "act"
        if masked:
            ctx.log("contract.masked_criteria", criteria=masked)
        if masked_steps:
            ctx.log("contract.masked_steps", steps=masked_steps)
        ctx.log("plan.submit", goal=st.goal, facts=facts, success_criteria=st.success_criteria, steps=steps,
                draft=shown, seconds=round(seconds, 1), replan=False)
        for f in facts:
            self.evidence.add(f["value"])
        loop.questions = [str(q).strip() for q in data.get("questions") or [] if str(q).strip()]
        loop.contract_done = True
        return Result(f"Task contract written: {len(steps)} steps, {len(facts)} facts",
                      f"Contract written: {len(steps)} steps, {len(facts)} facts")

    def _recall(self, ctx: Ctx, loop: Loop, documents: str) -> str:
        """MEMORY, part 1: the standing instructions this task touches on (request and documents), shown to the
        contract and to every act turn. Guidance only: the handbook and the policy gate still decide."""
        st = ctx.state
        if ctx.lessons is None:
            return ""
        found = ctx.lessons.relevant(f"{st.request}\n{contract.index_from(loop.msgs)}\n{documents[:4000]}")
        st.lessons = [x.line() for x in found]
        if found:
            ctx.log("lesson.recalled", ids=[x.id for x in found])
        return prompts.LESSONS_BLOCK.format(lines="\n".join(f"- {x}" for x in st.lessons)) if found else ""

    def _from_memory(self, ctx: Ctx, question: str) -> Result | None:
        """MEMORY, part 2: before a question reaches a person, is it one they already answered and asked Nova to
        remember? Word overlap proposes candidates; one small schema call confirms one really answers this case. A
        match is acted on, and recorded as asked and answered by that standing instruction."""
        st = ctx.state
        if ctx.lessons is None or st.waiting_for or not question:
            return None
        found = ctx.lessons.candidates(question)
        if not found:
            return None
        try:
            reply = self.llm.chat([{"role": "system", "content": prompts.LESSON_SYSTEM},
                                   {"role": "user", "content": prompts.LESSON_MATCH.format(
                                       requester=st.requester, question=question,
                                       lessons="\n".join(f"- {x.line()}" for x in found))}],
                                  schema=prompts.LESSON_SCHEMA, think=False)
            pick = str(json.loads(reply.content or "{}").get("answers", "none")).strip()
        except (ValueError, TypeError, LLMError) as e:
            ctx.log("runtime.warning", message=f"memory check failed: {e}")
            return None
        lesson = next((x for x in found if x.id == pick), None)
        ctx.log("lesson.match", question=question, candidates=[x.id for x in found], picked=pick)
        if lesson is None:
            return None
        ctx.lessons.used(lesson.id)
        st.questions.append({"question": question, "answer": lesson.guidance,
                             "by": f"{lesson.by}, standing instruction {lesson.id} ({lesson.created_at[:10]})"})
        ctx.log("lesson.used", id=lesson.id, question=question, guidance=lesson.guidance, by=lesson.by)
        return Result(f"{lesson.by} already answered this and asked Nova to remember it ({lesson.id}, given on "
                      f"{lesson.created_at[:10]}): \"{lesson.guidance}\" Act on that. Ask again only if this case "
                      "differs in a way that matters.", f'Answered by standing instruction {lesson.id}: '
                      f'"{lesson.guidance[:800]}"')  # whole: a cut answer reads as unfinished

    def _decide_split(self, ctx: Ctx, loop: Loop) -> list[str]:
        """Before the contract, when several emails were read: which of them does the request ask to handle, each on
        its own? One small schema-constrained call over a one-line list of the emails (inside the big contract prompt
        the answer changed from run to run). Returns the items' requests, or [] for one matter."""
        st = ctx.state
        index = contract.index_from(loop.msgs)
        read = sorted({int(n) for n in re.findall(r"message #(\d+)", index)})
        if st.parent or len(read) < 2:
            return []
        try:
            reply = self.llm.chat([{"role": "system", "content": prompts.SPLIT_SYSTEM},
                                   {"role": "user", "content": prompts.SPLIT_DECIDE.format(
                                       requester=st.requester, request=st.request, index=index)}],
                                  schema=prompts.SPLIT_SCHEMA, think=False)
            picked = [int(n) for n in json.loads(reply.content or "{}").get("emails", [])]
        except (ValueError, TypeError, LLMError) as e:
            ctx.log("runtime.warning", message=f"split decision failed: {e}")
            return []
        picked = [n for n in dict.fromkeys(picked) if n in read]
        ctx.log("work.decide", read=read, picked=picked)
        return [self._item_request(f"#{n}", loop) for n in picked][:10] if len(picked) >= 2 else []

    @staticmethod
    def _item_request(item: str, loop: Loop) -> str:
        """An item's request, from the system of record: the email it names, with its real sender and subject. The
        model's own description stays out (seen 2026-10-08: "ALREADY PAID per tracker row 33" sent the item down the
        refund procedure; another item carried a wrong PO into its goal)."""
        ref = re.search(r"#\s*(\d+)", item)
        if not ref:
            return item
        for m in loop.msgs:
            seen = re.search(rf'message #{ref[1]} from ([^:]+): "([^"]+)"', str(m.get("summary") or ""))
            if m.get("tool_name") == "mail_read" and seen:
                return f'Process email #{ref[1]} from {seen[1]}: "{seen[2]}"'
        return f"Process email #{ref[1]}"

    @staticmethod
    def _items(st: RunState, data: dict) -> list[str]:
        """The separate documents a contract says the request covers (2–10), or [] (one document, or already an item)."""
        items = list(dict.fromkeys(str(i).strip() for i in data.get("work_items") or [] if str(i).strip()))
        return items[:10] if len(items) >= 2 and not st.parent else []

    def _directory_hint(self, ctx: Ctx, unsupported: list[str]) -> str:
        """For a recipient nobody mentioned and nothing like a known one ("procurement@…", seen 2026-10-09, resent five
        times): who the company's directory says is who, so the person can be chosen by role."""
        if not any(u.startswith("recipient ") and "did you mean" not in u for u in unsupported):
            return ""
        people: dict[str, list[str]] = {}
        for who, addr in self._roles("\n".join(ctx.handbook.docs.values())).items():
            people.setdefault(addr, []).append(who)
        lines = [f"{', '.join(names)}: {addr}" for addr, names in people.items()]
        return (" The company's people, from the handbook: " + "; ".join(lines) + ".") if lines else ""

    @staticmethod
    def _roles(text: str) -> dict[str, str]:
        """Who is who, from the handbook's own tables ("| Ananya Iyer | Procurement Lead | ananya.iyer@… |"): each
        short name or role on a row with an address → that address."""
        out: dict[str, str] = {}
        for line in text.splitlines():
            found = provenance.EMAIL.findall(line)
            if "|" not in line or len(found) != 1:
                continue
            for cell in (c.strip() for c in line.strip().strip("|").split("|")):
                if cell and cell != found[0] and len(cell) <= 30 and not re.search(r"[\d;,@*]", cell):
                    out.setdefault(cell, found[0].lower())  # as the handbook writes it; matched without case
        return out

    @staticmethod
    def _mask(texts: list[str], known: provenance.Evidence, people: provenance.Evidence | None = None,
              roles: dict[str, str] | None = None) -> tuple[list[str], list[dict]]:
        """Each text with the dates, IDs and amounts that `known` doesn't contain replaced by "(as worked out)";
        and what was replaced, for the log. `people`: where to find a named person's address (the whole handbook,
        the company's directory, which may hold people the task's own documents don't)."""
        out, masked = [], []
        directory = sorted(known.emails | (people.emails if people else set()))
        for t in texts:
            # a misspelt address becomes the one the documents have (seen 2026-10-09: "Email sent to anya.iyer@…" in
            # the criteria, so the verifier confirmed the email that went to the wrong person)
            for e in set(provenance.EMAIL.findall(t or "")):
                near = "" if e.lower() in known.emails else known.closest(e)
                if near:
                    t = t.replace(e, near)
                    masked.append({"text": t, "values": [f"{e} → {near}"]})
            # a person named without an address gets theirs ("with Rahul Mehta in cc"), so the verifier can check that
            # they received it (seen 2026-10-09: the cc was forgotten, and a name gave the check nothing to look for)
            for addr in directory:
                name = addr.split("@")[0].replace(".", " ")
                if " " in name and addr not in (t or "").lower():
                    t = re.sub(rf"\b({re.escape(name)})\b(?! \()", rf"\1 ({addr})", t or "", count=1, flags=re.I)
            # a role the criterion tells something ("the Procurement Lead informed", "email … to the Procurement Lead")
            # gets that person's address too (seen 2026-10-09: three T2 runs never told the Procurement Lead, and the
            # verifier couldn't see it); a role named for an approval ("with AP Manager approval") doesn't
            for role, addr in (roles or {}).items():
                if addr in (t or "").lower():
                    continue
                r = re.escape(role)
                m = re.search(rf"\b(?:inform|informed|tell|told|notify|notified|email|emailed|to|cc|copied)\b"
                              rf"(?:\W+\w+){{0,3}}?\W+(?:the\s+)?({r})\b(?!\s*(?:'s\s+)?approv)"
                              rf"|\b({r})\b\s+(?:is\s+|was\s+)?(?:informed|told|notified|in cc|cc'd|copied)",
                              t or "", re.I)
                if m and "approv" not in m.group(0).lower():
                    end = m.end(1) if m.group(1) else m.end(2)
                    t = f"{t[:end]} ({addr}){t[end:]}"
            invented = [v for v in (known.missing(t) if t else []) if v.lower() not in directory]
            for v in invented:
                t = re.sub(r"(?:₹\s?|rs\.?\s?|inr\s?)?" + re.escape(v), "(as worked out)", t, flags=re.I)
            if invented:
                masked.append({"text": t, "values": invented})
            if t:
                out.append(t)
        return out, masked

    def _enter_act(self, ctx: Ctx, loop: Loop) -> None:
        """ACT starts from a clean context (request, procedures, documents, contract); the contract's questions for
        the requester go first, as calls that can wait for an answer like any other."""
        st, hb = ctx.state, ctx.handbook
        handbook = "\n\n".join(f"=== Handbook: {d} ({hb.titles[d]}) ===\n{hb.docs[d].strip()}" for d in st.handbook_docs)
        body = prompts.ACT.format(requester=st.requester, request=st.request, handbook=handbook,
                                  documents=contract.documents_from(loop.msgs, limit_each=2500, limit_total=4000),
                                  goal=st.goal, criteria="\n".join(f"- {c}" for c in st.success_criteria) or "- (none)",
                                  lessons=prompts.LESSONS_BLOCK.format(lines="\n".join(f"- {x}" for x in st.lessons))
                                  if st.lessons else "")
        loop.msgs = [{"role": "system", "content": loop.system}, {"role": "user", "content": body}]
        loop.recent, loop.fails, loop.contract_done, loop.pending = [], 0, False, []
        if loop.questions:
            asks = [ToolCall("ask_human", {"question": q}) for q in loop.questions]
            loop.msgs.append({"role": "assistant", "content": "", "tool_calls": asks})
            loop.pending, loop.questions = [asdict(c) for c in asks], []

    # ------------------------------------------------------------------ the model's view
    def _status(self, st: RunState, notes: list[str]) -> str:
        if st.phase == "understand":
            body = "[status] Still understanding the task: read what you need, then call submit_plan."
        else:
            lines = [f"[status] Goal: {st.goal}"]
            if st.facts:
                lines.append("Facts from the documents:")
                lines += [f"- {f['name']}: {f['value']}" for f in st.facts]
            lines += ["Plan:", plan_text(st)]
            nxt = st.next_step()
            lines.append(f"Next: step {nxt} — {st.plan[nxt - 1].text}" if nxt else "Every step is done: call finish.")
            sec = self._section(st.plan[nxt - 1].guide) if nxt else None
            if sec:
                lines.append(f"What the handbook says for this step ({sec.doc} › {sec.heading or sec.title}):\n{sec.text}")
                for ref in self._referenced(sec, st.plan[nxt - 1].text):
                    lines.append(f"It refers to {ref.doc} › {ref.heading or ref.title}:\n{ref.text}")
            if st.approvals:
                lines.append("Approvals: " + "; ".join(
                    f"{a.ref} {a.status}{' (used for ' + a.consumed_by + ')' if a.consumed_by else ''} ({a.kind}: "
                    + ", ".join(f"{k}={v}" for k, v in a.payload.items()) + ")" for a in st.approvals))
            if st.questions:  # answers whole: cut, they read as unfinished
                lines.append("Answers from people: " + "; ".join(f'"{q["question"][:80]}" → "{q["answer"][:800]}"'
                                                                 for q in st.questions))
            body = "\n".join(lines)
        return body + "".join(f"\n[runtime] {n}" for n in notes)

    def _estimate(self, prompt: list[dict], reg: Registry, st: RunState) -> int:
        chars = sum(len(m.get("content") or "") + len(json.dumps([[c.name, c.arguments] for c in m.get("tool_calls")
                                                                   or []], default=str)) for m in prompt)
        return int((chars + len(json.dumps(reg.schemas(st.phase)))) / CHARS_PER_TOKEN)

    def _render(self, msgs: list[dict], st: RunState, notes: list[str], reg: Registry) -> tuple[list[dict], int]:
        """The bounded view the model gets: the last k tool results in full, older ones as their summary line,
        and the plan status after the newest message. Shrinks k (then folds old turns) to fit the context."""
        limit = int(self.num_ctx * 0.75)
        for k in range(self.s.keep_full, 0, -1):
            view = self._view(msgs, st, notes, k, fold_before=None)
            if self._estimate(view, reg, st) <= limit:
                return view, k
        tool_idx = [i for i, m in enumerate(msgs) if m["role"] == "tool"]
        for keep_turns in (20, 12, 6, 3):
            if len(tool_idx) <= keep_turns:
                continue
            cut = tool_idx[-keep_turns]
            while cut > 2 and msgs[cut]["role"] != "assistant":
                cut -= 1
            view = self._view(msgs, st, notes, 1, fold_before=cut)
            if self._estimate(view, reg, st) <= limit:
                return view, 1
        return self._view(msgs, st, notes, 1, fold_before=tool_idx[-1] if tool_idx else None), 1

    def _view(self, msgs: list[dict], st: RunState, notes: list[str], k: int, fold_before: int | None) -> list[dict]:
        tool_idx = [i for i, m in enumerate(msgs) if m["role"] == "tool"]
        full = set(tool_idx[-k:])
        out: list[dict] = []
        folded: list[str] = []
        for i, m in enumerate(msgs):
            if fold_before is not None and 2 <= i < fold_before:
                if m["role"] == "tool":
                    folded.append(f"- {m['summary']}")
                continue
            if fold_before is not None and i == fold_before and folded:
                out.append({"role": "user", "content": "[runtime] Earlier actions in this task (one line each):\n"
                                                       + "\n".join(folded)})
            if m["role"] == "tool":
                text = m["content"] if i in full else f"(older result; details hidden) {m['summary']}"
                out.append({"role": "tool", "tool_name": m["tool_name"], "tool_call_id": m["tool_call_id"],
                            "content": text})
            elif m["role"] == "assistant":
                out.append({"role": "assistant", "content": (m.get("content") or "")[:400],
                            "tool_calls": m.get("tool_calls")})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        if (len(out) > 2 or st.phase == "act") and out[-1]["role"] in ("tool", "user"):
            out[-1] = {**out[-1], "content": out[-1]["content"] + "\n\n" + self._status(st, notes)}
        return out

    # ------------------------------------------------------------------ the record
    def _close_run(self, ctx: Ctx, loop: Loop, status: str) -> None:
        st = ctx.state
        loop.elapsed = time.time() - self._t0
        run_dir = self.log.run_dir(st.run_id)
        if status == "split":  # the items run next; this run's record is written when they are done (_run_items)
            ctx.log("run.split", items=[i["request"] for i in st.work_items])
            self.log.update_run(st.run_id, status="split", state=st.to_dict())
            return
        if status == "waiting":
            snapshot = {"state": st.to_dict(), "loop": loop.to_dict(), "evidence": self.evidence.dump(),
                        "ctx": {"created_files": sorted(ctx.created_files), "sent": ctx.sent,
                                "contacts": sorted(ctx.contacts), "vendors": ctx.vendors, "rows": ctx.rows,
                                "done": ctx.done, "uncertain": sorted(ctx.uncertain)}}
            self.log.update_run(st.run_id, status="waiting", state=st.to_dict(), snapshot=snapshot)
        else:
            ctx.log("run.end", status=status, summary=st.summary,
                    verdict=(loop.verification or {}).get("verdict"))
            self.log.update_run(st.run_id, state=st.to_dict(), snapshot=None,
                                verdict=(loop.verification or {}).get("verdict"))
            self.log.end_run(st.run_id, status, st.summary)
        md, evidence = report.build(st, status, self.log.events(st.run_id), loop.verification, self.model)
        (run_dir / "facts.json").write_text(json.dumps(st.run_facts(), indent=2), encoding="utf-8")
        (run_dir / "evidence.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False, default=str),
                                               encoding="utf-8")
        (run_dir / "report.md").write_text(md, encoding="utf-8")
