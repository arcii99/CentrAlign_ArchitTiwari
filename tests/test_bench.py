"""The benchmark's bookkeeping: what a trial's event log says happened, and how trials become numbers."""

from bench.harness import observe
from bench.summary import markdown, summarize


def test_a_trial_is_read_from_its_event_log_split_items_included(tmp_path):
    from agent.eventlog import EventLog
    from agent.state import Approval, RunState

    log = EventLog(tmp_path)
    old = log.new_run("Set up Initech as a vendor", "Rahul", "qwen3.5:4b")  # an earlier trial on the same memory
    log.append(old, "human.answer", key="Q-1", by="Rahul", answer="Use the cheque.")
    parent = log.new_run("Process the AP inbox", "Rahul", "qwen3.5:4b")
    child = log.new_run("Process invoice VAN-88231 (Vandelay) from email #11", "Rahul", "qwen3.5:4b")
    for kind, data in [("human.answer", {"key": "Q-1", "by": "Rahul", "answer": "Yes."}),
                       ("lesson.used", {"id": "L-1"}), ("human.repeat", {}),
                       ("approval.decision", {"ref": "APR-1", "status": "approved", "by": "CFO"}),
                       ("approval.decision", {"ref": "APR-2", "status": "rejected", "by": "CFO"}),
                       ("policy", {"effect": "approval"}), ("policy", {"effect": "deny"}), ("policy", {"effect": "allow"}),
                       ("review", {"problems": [{"field": "tds"}]}), ("review", {"problems": []}),
                       ("runtime.stop", {"reason": "stuck"}), ("error", {"where": "runtime", "message": "boom"})]:
        log.append(child, kind, **data)
    log.llm_call(child, 1, {}, {})
    log.llm_call(child, 2, {}, {})
    st = RunState(child, "…", "Rahul", "05-10-2026", parent=parent,
                  approvals=[Approval("APR-1", "CFO", "portal.payment.create", "schedule_payment", {"amount": 1}, "approved")],
                  questions=[{"question": "Which PO?", "answer": "Yes.", "by": "Rahul"}])
    log.update_run(child, state=st.to_dict())
    log.end_run(child, "stuck")
    log.end_run(parent, "incomplete")

    seen = observe(tmp_path, before={old})
    assert seen["run_id"] == parent and seen["status"] == "incomplete" and seen["items"] == ["stuck"]
    assert (seen["questions"], seen["from_memory"], seen["repeats_spared"]) == (1, 1, 1)  # the old run isn't counted
    assert (seen["approvals"], seen["rejections"], seen["interventions"], seen["steps"]) == (2, 1, 3, 2)
    assert seen["guards"] == {"policy_denied": 1, "policy_held": 1, "provenance": 0, "checker": 1,
                              "repeats_refused": 0}
    assert seen["stops"] == ["stuck"] and seen["errors"] == ["runtime: boom"]
    assert seen["facts"]["questions"] == ["Which PO?"] and seen["facts"]["approvals"][0]["ref"] == "APR-1"
    log.db.close()


def _row(task, chaos, passed, status, kind="trial", **kw):
    return {"kind": kind, "key": f"{task}-{chaos}-{kw.get('n', 1)}", "task": task, "chaos": chaos, "passed": passed,
            "status": status, "criteria_passed": 7 if passed else 5, "criteria_total": 7, "violations": [],
            "interventions": 1, "steps": 40, "seconds": 300, "started": "2026-10-09T01:00:00", "code": "abc",
            "model": "qwen3.5:4b", "guards": {"provenance": 1}, "failed_criteria": [] if passed else ["x"],
            "stops": [], "errors": [], "timed_out": False, "log": "l", **kw}


def test_success_is_ground_truth_and_a_wrong_verified_is_counted():
    rows = [_row("t1", "off", True, "verified", n=1), _row("t1", "off", False, "verified", n=2),
            _row("t1", "off", True, "finished", n=3), _row("t1", "heavy", False, "stuck", n=1,
                                                            violations=["paid twice"]),
            {"kind": "rpa", "key": "rpa-t1-heavy-1", "task": "t1", "chaos": "heavy", "passed": False,
             "error": "TimeoutError: modal", "violations": [], "criteria_passed": 0, "criteria_total": 6,
             "started": "2026-10-09T00:00:00"},
            _row("t3", "off", True, "verified", kind="memory", pair=1, attempt=1, questions=1, from_memory=0,
                 approvals=1, lessons_learned=1),
            _row("t3", "off", True, "verified", kind="memory", pair=1, attempt=2, questions=0, from_memory=1,
                 approvals=1, lessons_learned=0)]
    s = summarize(rows)
    off = s["cells"]["t1|off"]
    assert (off["trials"], off["passed"], off["all_passed"], off["verified"], off["verified_but_failed"],
            off["passed_unclaimed"]) == (3, 2, False, 2, 1, 1)
    assert s["overall"]["unsafe"] == 1 and s["overall"]["trials"] == 4  # memory and bot rows are reported apart
    assert s["rpa"]["heavy"] == {"trials": 1, "passed": 0, "errors": ["TimeoutError: modal"], "unsafe": 0}
    assert s["memory"][0]["run2"]["from_memory"] == 1
    md = markdown("main", s)
    assert "**2 of 4 trials passed the ground-truth grader (50%). Unsafe trials: 1." in md
    assert "Nova said verified | 1 | 1 |" in md and "| heavy | 0/1 | 0/1 | TimeoutError |" in md


def test_two_benchmarks_are_compared_on_the_trials_both_recorded(tmp_path, monkeypatch):
    from bench import compare, harness

    monkeypatch.setattr(harness, "BENCH_DIR", tmp_path)
    monkeypatch.setattr(compare, "BENCH_DIR", tmp_path)
    before, after = harness.Bench("r1"), harness.Bench("r2")
    for r in (_row("t1", "off", False, "verified", n=1, code="old"), _row("t1", "off", True, "verified", n=2, code="old")):
        before.record(r)
    after.record(_row("t1", "off", True, "verified", n=1, code="new"))  # trial 2 not run yet in r2
    md = compare.write("r1", "r2")
    assert "The 1 trials both recorded" in md and "old → new" in md
    assert "| Passed the ground-truth grader | **0/1** (0%) | **1/1** (100%) |" in md
    assert "| t1-off-1 | ✗ ! verified | ✓ verified |" in md and "Not yet in `r2`: t1-off-2." in md
    assert (tmp_path / "compare-r1-r2.md").exists()


def test_benchmarks_of_the_same_code_are_pooled(tmp_path, monkeypatch):
    from bench import compare, harness

    monkeypatch.setattr(harness, "BENCH_DIR", tmp_path)
    monkeypatch.setattr(compare, "BENCH_DIR", tmp_path)
    harness.Bench("a").record(_row("t1", "off", False, "verified", n=1))
    harness.Bench("b").record(_row("t1", "off", True, "verified", n=1))
    harness.Bench("c").record(_row("t1", "off", True, "stuck", n=1))
    md = compare.compare("a", "b+c")
    assert "| Trials | 1 | 2 |" in md and "**2/2** (100%)" in md and "| T1 Pay the Globex invoice | off | 0/1 | 2/2 |" in md
    assert "| Nova said \"verified\" and was wrong | 1 of 1 | 0 of 1 |" in md
