"""Lightweight event/ΔState bus for agent coordination.

Agents publish small, versioned events (``RESOURCE_UPDATED``, ``AGENT_RUN``,
...) instead of transporting full history between each other. Consumers read
deltas through a cursor. Publishing is gated on an authorized actor (SDK-001
identity boundary) and is deterministic code — it never re-invokes a model and
does not sit on the Decision/Tool governance chain reserved for workspace
mutation.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VialEvent:
    event_id: str
    type: str
    resource: str
    version: int
    actor: str
    timestamp: float
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type,
            "resource": self.resource,
            "version": self.version,
            "actor": self.actor,
            "timestamp": self.timestamp,
            "data": dict(self.data),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "VialEvent":
        return cls(
            event_id=str(value["event_id"]),
            type=str(value["type"]),
            resource=str(value["resource"]),
            version=int(value["version"]),
            actor=str(value["actor"]),
            timestamp=float(value["timestamp"]),
            data=dict(value.get("data") or {}),
        )


class EventStore:
    """Ordered, idempotent event log keyed by (type, resource, version).

    Republishing the same (type, resource, version) returns the existing event,
    so network/agent retries cannot duplicate records.
    """

    def __init__(self) -> None:
        self.events: dict[str, VialEvent] = {}
        self._order: list[str] = []
        self.authorized_actors: set[str] = set()

    def configure(self, authorized: set[str]) -> None:
        self.authorized_actors = set(authorized)

    def publish(self, type: str, resource: str, version: int,
                actor: str, data: dict[str, Any] | None = None) -> VialEvent:
        if actor not in self.authorized_actors:
            raise PermissionError(
                f"actor {actor} is not authorized to publish events")
        key = f"{type}:{resource}:{version}"
        existing = self.events.get(key)
        if existing is not None:
            return existing
        event = VialEvent(
            event_id=f"EVT-{uuid.uuid4().hex[:12]}",
            type=type, resource=resource, version=version, actor=actor,
            timestamp=time.time(), data=dict(data or {}))
        self.events[key] = event
        self._order.append(key)
        return event

    def latest(self, resource: str | None = None,
               type: str | None = None) -> VialEvent | None:
        for key in reversed(self._order):
            event = self.events[key]
            if resource is not None and event.resource != resource:
                continue
            if type is not None and event.type != type:
                continue
            return event
        return None

    def delta(self, after_event_id: str = "") -> list[VialEvent]:
        if not after_event_id:
            return [self.events[key] for key in self._order]
        positions = {
            event.event_id: index
            for index, event in enumerate(self.events.values())
        }
        start = positions.get(after_event_id)
        if start is None:
            return [self.events[key] for key in self._order]
        return [self.events[key] for key in self._order[start + 1:]]

    def version_of(self, resource: str) -> int:
        latest = self.latest(resource)
        return latest.version if latest is not None else 0

    def stats(self) -> dict[str, Any]:
        return {
            "events": len(self._order),
            "resources": len({event.resource for event in self.events.values()}),
        }

    def to_list(self) -> list[dict[str, Any]]:
        return [self.events[key].to_dict() for key in self._order]

    @classmethod
    def from_list(cls, values: list[dict[str, Any]]) -> "EventStore":
        store = cls()
        for value in values:
            event = VialEvent.from_dict(value)
            key = f"{event.type}:{event.resource}:{event.version}"
            store.events[key] = event
            store._order.append(key)
        return store