"""Presentation state for the TUI; governance remains in the Runtime."""
from __future__ import annotations

from dataclasses import dataclass, field

PIPELINE = (
    "TASK", "CONTEXT", "COGNITION", "DECISION", "CONSENSUS",
    "AUTHORIZATION", "PATCH", "VALIDATION", "APPLY", "TEST", "COMMIT",
)


@dataclass
class TUIState:
    task_id: str | None = None
    task: str = ""
    stage: str = "IDLE"
    status: str = "READY"
    risk: str = "medium"
    decision_id: str | None = None
    consensus_ratio: float | None = None
    authorization: str = "UNKNOWN"
    patch_status: str = "PENDING"
    test_status: str = "PENDING"
    latency_seconds: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None
    failure_type: str = ""
    events: list[str] = field(default_factory=list)

    def start(self, task: str) -> None:
        self.task = task
        self.task_id = None
        self.stage = "TASK"
        self.status = "RUNNING"
        self.patch_status = "PENDING"
        self.test_status = "PENDING"
        self.failure_type = ""
        self.events = ["task started"]

    def advance(self, stage: str, event: str | None = None) -> None:
        self.stage = stage
        if event:
            self.events.append(event)

    def finish(self, passed: bool = True, event: str | None = None) -> None:
        self.status = "DONE" if passed else "FAILED"
        if event:
            self.events.append(event)

    def pipeline(self) -> list[tuple[str, str]]:
        current = PIPELINE.index(self.stage) if self.stage in PIPELINE else -1
        return [
            (stage, "done" if index < current else
             "running" if index == current and self.status == "RUNNING" else
             "failed" if index == current and self.status == "FAILED" else "pending")
            for index, stage in enumerate(PIPELINE)
        ]
