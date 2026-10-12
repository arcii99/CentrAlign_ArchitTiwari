"""A run's state: everything the runtime holds for the model (contract, plan, approvals, answers).

The model never has to remember these; the runtime shows them back to it after every action. The state is
plain data so it can be snapshotted after every step (and, in Phase 2, resumed after a restart).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Step:
    text: str
    status: str = "todo"  # todo | done | skipped
    result: str = ""
    guide: str = ""  # the handbook section this step carries out ("doc|heading"), shown when it is next


@dataclass
class Approval:
    ref: str
    approver: str
    action: str  # canonical action name, e.g. portal.payment.create
    kind: str  # what the model asked for, e.g. schedule_payment
    payload: dict
    status: str  # approved | rejected
    by: str = ""
    comment: str = ""
    consumed_by: str = ""  # the record or message it authorised: an approval is used once


@dataclass
class RunState:
    run_id: str
    request: str
    requester: str
    today: str  # DD-MM-YYYY
    phase: str = "understand"  # understand | act | done
    goal: str = ""
    facts: list[dict] = field(default_factory=list)  # {"name", "value", "source", "grounded"} from the contract
    success_criteria: list[str] = field(default_factory=list)
    plan: list[Step] = field(default_factory=list)
    approvals: list[Approval] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)  # {"question", "answer"}
    handbook_docs: list[str] = field(default_factory=list)
    summary: str = ""
    finished: bool = False
    waiting_for: str = ""  # the inbox item (question or approval) the run is paused on
    correcting: bool = False  # the work was reported done and the verifier disagreed: changes now are corrections
    parent: str = ""  # the run this one is an item of (a request split into one run per document)
    work_items: list[dict] = field(default_factory=list)  # a split request: {"request", "run_id", "status"} each
    lessons: list[str] = field(default_factory=list)  # standing instructions recalled for this task (shown to it)

    def next_step(self) -> int | None:
        """1-based number of the first open step."""
        return next((i for i, s in enumerate(self.plan, 1) if s.status == "todo"), None)

    def open_steps(self) -> list[int]:
        return [i for i, s in enumerate(self.plan, 1) if s.status == "todo"]

    def run_facts(self) -> dict:
        """What the ground-truth checker's RunFacts needs: approvals granted and questions put to people."""
        return {"approvals": [{"ref": a.ref, "action": a.action, "payload": a.payload, "status": a.status}
                              for a in self.approvals],
                "questions": [q["question"] for q in self.questions]}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RunState":
        d = dict(d)
        d["plan"] = [Step(**s) for s in d.get("plan", [])]
        d["approvals"] = [Approval(**a) for a in d.get("approvals", [])]
        return cls(**d)
