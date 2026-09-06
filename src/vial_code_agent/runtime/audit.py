"""Auditability + telemetry (RFC-004, SDK-005, TOOLS-001)."""
from __future__ import annotations

from typing import Any


class AuditTelemetryMixin:
    """Audit and telemetry methods for VialRuntime."""

    def audit_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for tool in self.tools.list():  # type: ignore[attr-defined]
            records.extend(record.__dict__ for record in tool.audit_records)
        return sorted(records, key=lambda record: record["timestamp"])

    def memory(self) -> dict[str, Any]:
        return {
            "reuse": self.reuse_stats(),  # type: ignore[attr-defined]
            "decisions": [
                {"id": d.id, "objective": d.objective, "status": d.status,
                 "outcome": d.outcome}
                for d in self.decision_engine.history(self.org_id)],  # type: ignore[attr-defined]
            "audit_records": len(self.patch_tool.audit_records),  # type: ignore[attr-defined]
            "approvals": [record.__dict__ for record in self.approvals.values()],  # type: ignore[attr-defined]
            "consensus_records": [  # type: ignore[attr-defined]
                record.__dict__ for record in self.consensus_records.values()],  # type: ignore[attr-defined]
            "state_root": str(self.state_root),  # type: ignore[attr-defined]
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "organization_id": self.organization.org_id,  # type: ignore[attr-defined]
            "authority": self.organization.authority,  # type: ignore[attr-defined]
            "config_version": self.organization.config_version,  # type: ignore[attr-defined]
            "state_version": self.organization.state_version,  # type: ignore[attr-defined]
            "resources": [resource.to_dict() for resource in self.registry.list()],  # type: ignore[attr-defined]
            "tools": [tool.to_dict() for tool in self.tools.list()],  # type: ignore[attr-defined]
            "reuse": self.reuse_stats(),  # type: ignore[attr-defined]
            "coordinator": {
                "intents": len(self.coordinator.intents),  # type: ignore[attr-defined]
                "duplicate_commits": self.coordinator.duplicate_commits,  # type: ignore[attr-defined]
                "interruptions": self.coordinator.interruptions,  # type: ignore[attr-defined]
            },
            "decisions": len(self.decision_engine.decisions),  # type: ignore[attr-defined]
            "executions": len(self.executions),  # type: ignore[attr-defined]
            "audit_records": len(self.patch_tool.audit_records),  # type: ignore[attr-defined]
            "contexts": len(self.contexts),  # type: ignore[attr-defined]
            "costs": self.costs(),  # type: ignore[attr-defined]
            "memory": self.memory(),
            "events": self.events.stats(),  # type: ignore[attr-defined]
            "project": (self.project.snapshot.to_dict()  # type: ignore[attr-defined]
                        if self.project.snapshot is not None else None),  # type: ignore[attr-defined]
            "persisted": self.persist_state,  # type: ignore[attr-defined]
            "state_root": str(self.state_root),  # type: ignore[attr-defined]
        }
