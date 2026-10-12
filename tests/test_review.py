"""Maker-checker by re-derivation: the checker's tool calls run, and only real disagreements on dates/amounts count."""

from agent.llm import Reply, ToolCall
from agent.review import disagreements, rederive, reviewable


class Checker:
    """A stand-in checker model: computes the due date with date_calc, then answers."""

    def __init__(self, answer: dict):
        self.answer, self.calls = answer, 0

    def chat(self, messages, tools=None, think=False, max_tokens=None, **kw):
        self.calls += 1
        if self.calls == 1:
            return Reply("", [ToolCall("date_calc", {"date": "30-Sep-2026", "add_days": 30})])
        return Reply("", [ToolCall("answer", {"values": self.answer})])


def test_rederive_runs_the_tools_and_returns_the_answer():
    seen = []
    tools = {"date_calc": lambda a: seen.append(a) or "30-09-2026 + 30 days = 30-10-2026 (Friday)"}
    values, trace = rederive(Checker({"Due date": "30-10-2026"}), tools, "submit the bill", ["Due date"],
                             "05-10-2026", "Due date = invoice date + terms", "- Invoice Date: 30-Sep-2026", "", "")
    assert values == {"Due date": "30-10-2026"} and trace == ["30-09-2026 + 30 days = 30-10-2026 (Friday)"]
    assert seen == [{"date": "30-Sep-2026", "add_days": 30}]


def test_only_real_disagreements_on_dates_and_amounts_count():
    maker = reviewable({"Vendor": "V-1001", "Due date": "05-10-2026", "Net payable": "174000", "Payment method": "NEFT",
                        "Taxable value": "1,50,000.00"})
    assert set(maker) == {"Due date", "Net payable", "Taxable value"}  # identifiers and choices are not re-derived
    checker = {"due date": "30-Oct-2026", "Net payable": "1,77,480.00", "Taxable value": "see invoice"}
    trace = ["30-09-2026 + 30 days = 30-10-2026 (Friday), 25 day(s) after today (05-10-2026)"]
    # the due date was worked out with date_calc; the net payable was not (made up in the answer), so it is ignored
    assert disagreements(maker, checker, trace) == [{"field": "Due date", "value": "05-10-2026",
                                                     "checker": "30-Oct-2026"}]
    assert disagreements({"Due date": "30-10-2026"}, checker, trace) == []  # same date, other format: agreement
