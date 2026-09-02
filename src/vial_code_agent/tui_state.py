"""Presentation state for the TUI; governance remains in the Runtime."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

PIPELINE = (
    "TASK", "AGENT", "CONSENSUS", "EVIDENCE", "AUTHORIZATION", "PATCH",
    "SANDBOX", "TESTS", "COMMIT",
)


@dataclass(frozen=True)
class PipelineEvent:
    """A factual lifecycle observation consumed by the presentation state."""

    stage: str
    status: str
    detail: str = ""
    timestamp: float = field(default_factory=time.time)


_EVENT_STAGE_ALIASES = {
    "CONTEXT": "AGENT",
    "COGNITION": "AGENT",
    "DECISION": "AUTHORIZATION",
    "VALIDATION": "PATCH",
    "APPLY": "PATCH",
    "TEST": "TESTS",
}


@dataclass
class TUIState:
    task_id: str | None = None
    task: str = ""
    base_commit: str = ""
    route: str = ""
    stage: str = "IDLE"
    status: str = "READY"
    risk: str = "medium"
    decision_id: str | None = None
    consensus_ratio: float | None = None
    authorization: str = "UNKNOWN"
    patch_status: str = "PENDING"
    patch_validation: str = "PENDING"
    retry: str = "NONE"
    environment: str = "UNKNOWN"
    test_status: str = "PENDING"
    latency_seconds: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None
    failure_type: str = ""
    failure_class: str = ""
    final_result: str = "PENDING"
    events: list[str] = field(default_factory=list)
    observations: list[PipelineEvent] = field(default_factory=list)

    def start(self, task: str) -> None:
        self.task = task
        self.task_id = None
        self.base_commit = ""
        self.route = ""
        self.stage = "TASK"
        self.status = "RUNNING"
        self.patch_status = "PENDING"
        self.patch_validation = "PENDING"
        self.retry = "NONE"
        self.environment = "UNKNOWN"
        self.test_status = "PENDING"
        self.failure_type = ""
        self.failure_class = ""
        self.final_result = "PENDING"
        self.events = ["task started"]
        self.observations = [PipelineEvent("TASK", "running", "task started")]

    def advance(self, stage: str, event: str | None = None) -> None:
        """Compatibility wrapper for callers migrating to ``observe``."""
        self.observe(PipelineEvent(stage, "running", event or ""))

    def observe(self, observation: PipelineEvent) -> None:
        stage = _EVENT_STAGE_ALIASES.get(observation.stage, observation.stage)
        if stage not in PIPELINE:
            if stage == "ENVIRONMENT":
                self.environment = observation.detail or observation.status.upper()
            elif stage == "RETRY":
                self.retry = observation.detail or observation.status.upper()
            return
        normalized = PipelineEvent(stage, observation.status,
                                   observation.detail, observation.timestamp)
        self.observations.append(normalized)
        self.stage = stage
        if observation.detail:
            self.events.append(observation.detail)
        if observation.status in {"failed", "blocked"}:
            self.status = "FAILED"
            self.failure_type = observation.detail or observation.status.upper()
            self.failure_class = observation.detail or observation.status.upper()
        elif observation.status == "completed":
            if stage == "PATCH":
                self.patch_status = "READY"
                self.patch_validation = "PASSED"
            if stage == "TESTS":
                self.test_status = "PASSED"
        elif observation.status == "running":
            self.status = "RUNNING"
        if stage == "AUTHORIZATION":
            self.authorization = observation.status.upper()

    def observe_runtime_event(self, event: object) -> None:
        """Project explicitly tagged runtime events into the TUI state."""
        data = getattr(event, "data", {}) or {}
        stage = data.get("pipeline_stage")
        if not stage:
            return
        self.observe(PipelineEvent(
            str(stage), str(data.get("pipeline_status", "completed")),
            str(data.get("detail", getattr(event, "type", "runtime event"))),
            float(getattr(event, "timestamp", time.time())),
        ))

    def finish(self, passed: bool = True, event: str | None = None) -> None:
        self.status = "DONE" if passed else "FAILED"
        self.final_result = "PASSED" if passed else "FAILED"
        if event:
            self.events.append(event)

    def event_line(self) -> str:
        return " | ".join(self.events[-3:]) if self.events else "ready"

    def pipeline(self) -> list[tuple[str, str]]:
        current = PIPELINE.index(self.stage) if self.stage in PIPELINE else -1
        completed = {
            event.stage for event in self.observations
            if event.status == "completed"
        }
        failed = {
            event.stage for event in self.observations
            if event.status in {"failed", "blocked"}
        }
        return [
            (stage, "failed" if stage in failed or
             (index == current and self.status == "FAILED") else
             "done" if stage in completed or index < current else
             "running" if index == current and self.status == "RUNNING" else
             "pending")
            for index, stage in enumerate(PIPELINE)
        ]
