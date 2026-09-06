"""Event/ΔState bus + materialized project state (agent coordination)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..events import VialEvent
from ..project import ProjectDelta, ProjectSnapshot


class EventProjectMixin:
    """Event publishing and project state management methods."""

    def publish_event(self, event_type: str, resource: str, version: int,
                      data: dict[str, Any] | None = None,
                      actor: str | None = None) -> VialEvent:
        event = self.events.publish(  # type: ignore[attr-defined]
            event_type, resource, version, actor or self.actor, data=data)  # type: ignore[attr-defined]
        self.persist()  # type: ignore[attr-defined]
        return event

    def event_delta(self, after_event_id: str = "") -> list[VialEvent]:
        return self.events.delta(after_event_id)  # type: ignore[attr-defined]

    def event_latest(self, resource: str | None = None,
                     event_type: str | None = None) -> VialEvent | None:
        return self.events.latest(resource, event_type)  # type: ignore[attr-defined]

    def capture_project(self, root: Path, files: list[Path]) -> ProjectSnapshot:
        snapshot = self.project.capture(root, files)  # type: ignore[attr-defined]
        self.project.restore(snapshot)  # type: ignore[attr-defined]
        self.persist()  # type: ignore[attr-defined]
        return snapshot

    def project_delta(self, root: Path, files: list[Path]) -> ProjectDelta | None:
        return self.project.delta_from(root, files)  # type: ignore[attr-defined]

    def set_project_status(self, module: str, value: str,
                           actor: str | None = None) -> None:
        self.project.set_status(module, value, actor or self.actor)  # type: ignore[attr-defined]
        self.persist()  # type: ignore[attr-defined]
