"""Nova's console: its pages render from Nova's records, and a person's decision lands in the inbox."""

from fastapi.testclient import TestClient


def test_console_pages_and_an_answer_from_the_inbox(tmp_path, monkeypatch):
    from agent.console import app as console
    from agent.eventlog import EventLog
    from agent.human import Inbox
    from agent.state import RunState
    from bench.harness import Bench
    from bench.summary import write

    monkeypatch.setenv("NOVA_RUNS_DIR", str(tmp_path / "runs"))
    log = EventLog(tmp_path / "runs")
    run = log.new_run("Set up Initech as a vendor", "Rahul Mehta (AP Manager)", "qwen3.5:4b")
    for kind, data in [("run.start", {"request": "Set up Initech as a vendor"}), ("llm.reply", {"n": 1}),
                       ("tool.call", {"name": "browser_fill", "arguments": {"fields": {"GSTIN": "29AAKFI6655L1ZB"}}}),
                       ("browser.post", {"action": "portal.vendor.create"}),
                       ("tool.result", {"ok": True, "summary": "Submitted \"Save vendor\" → /vendors/V-1008"}),
                       ("policy", {"effect": "approval", "action": "portal.bank_account.create",
                                   "rules": ["bank accounts need the AP Manager"]}),
                       ("lesson.used", {"id": "L-1", "by": "Rahul Mehta", "guidance": "<b>Use the cheque</b>"}),
                       ("human.question", {"key": "Q-1", "to": "Rahul Mehta (AP Manager)", "question": "Which one?"}),
                       ("run.pause", {"item": "Q-1", "waiting_for": "an answer from Rahul Mehta"})]:
        log.append(run, kind, **data)
    log.update_run(run, state=RunState(run, "Set up Initech as a vendor", "Rahul", "05-10-2026", goal="Onboard").to_dict())
    log.end_run(run, "failed")  # not "waiting": answering must not start a real run in this test
    Inbox(log.db).open("Q-1", run, "question", "Rahul Mehta (AP Manager)", {"question": "Form or cheque?"})
    log.db.close()

    bench = Bench.__new__(Bench)
    bench.name, bench.dir = "b1", tmp_path / "bench" / "b1"
    bench.results = bench.dir / "results.jsonl"
    bench.dir.mkdir(parents=True)
    bench.record({"kind": "trial", "key": "t3-off-1", "task": "t3", "chaos": "off", "passed": True, "status": "verified",
                  "criteria_passed": 5, "criteria_total": 5, "violations": [], "interventions": 2, "steps": 27,
                  "seconds": 236, "started": "2026-10-08T23:33:25", "code": "abc", "model": "qwen3.5:4b",
                  "guards": {}, "failed_criteria": [], "stops": [], "errors": [], "timed_out": False, "log": "l"})
    write(bench)
    monkeypatch.setattr(console, "BENCH_DIR", tmp_path / "bench")

    c = TestClient(console.app)
    page = c.get("/").text
    assert "Set up Initech as a vendor" in page and 'class="count">1<' in page  # one item waiting
    page = c.get(f"/runs/{run}").text
    assert "browser_fill" in page and "portal.vendor.create" in page and "Policy: approval" in page
    assert "&lt;b&gt;Use the cheque&lt;/b&gt;" in page  # whatever Nova saw is shown as text, never as markup
    assert "Form or cheque?" in c.get("/inbox").text
    assert "Waiting for a person" in page
    assert c.get(f"/runs/{run}/live?after=0").json()["status"] == "failed"
    r = c.post("/inbox/Q-1", data={"action": "answer", "text": "Use the cheque.", "remember": "1"}, follow_redirects=False)
    assert r.status_code == 303
    item = Inbox(EventLog(tmp_path / "runs").db).get("Q-1")
    assert (item["status"], item["answer"], item["remember"]) == ("answered", "Use the cheque.", 1)
    assert c.post("/inbox/Q-1", data={"action": "approve"}).status_code == 400  # a question isn't approved
    assert "No standing instructions yet" in c.get("/lessons").text
    assert "1/1" in c.get("/bench").text and "t3-off-1" in c.get("/bench/b1").text
    assert c.get("/bench/..%2Fx").status_code == 404
    assert c.get(f"/runs/{run}/files/report.md").status_code == 404  # not written yet
    assert c.get(f"/runs/{run}/files/runs.db").status_code == 404  # only the run's evidence files are served
