"""What every tool (and the browser) hands back to the runtime."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Result:
    text: str  # the full observation shown to the model: compact text, never raw HTML
    summary: str  # one line that stays in the model's history after the full text is hidden
    ok: bool = True  # False when the action failed or the system rejected it
    data: dict = field(default_factory=dict)  # structured details for the event log; never shown to the model

    @classmethod
    def error(cls, message: str, **data) -> "Result":
        return cls(text=f"ERROR: {message}", summary=f"ERROR: {message[:160]}", ok=False, data=data)
