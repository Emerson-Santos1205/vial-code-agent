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
        event = self.events.publish(
            event_type, resource, version, actor or self.actor, data=data)
        self.persist()
        return event

    def event_delta(self, after_event_id: str = "") -> list[VialEvent]:
        return self.events.delta(after_event_id)

    def event_latest(self, resource: str | None = None,
                     event_type: str | None = None) -> VialEvent | None:
        return self.events.latest(resource, event_type)

    def capture_project(self, root: Path, files: list[Path]) -> ProjectSnapshot:
        snapshot = self.project.capture(root, files)
        self.project.restore(snapshot)
        self.persist()
        return snapshot

    def project_delta(self, root: Path, files: list[Path]) -> ProjectDelta | None:
        return self.project.delta_from(root, files)

    def set_project_status(self, module: str, value: str,
                           actor: str | None = None) -> None:
        self.project.set_status(module, value, actor or self.actor)
        self.persist()
