"""Auditability + telemetry (RFC-004, SDK-005, TOOLS-001)."""
from __future__ import annotations

from typing import Any


class AuditTelemetryMixin:
    """Audit and telemetry methods for VialRuntime."""

    def audit_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for tool in self.tools.list():
            records.extend(record.__dict__ for record in tool.audit_records)
        return sorted(records, key=lambda record: record["timestamp"])

    def memory(self) -> dict[str, Any]:
        return {
            "reuse": self.reuse_stats(),
            "decisions": [
                {"id": d.id, "objective": d.objective, "status": d.status,
                 "outcome": d.outcome}
                for d in self.decision_engine.history(self.org_id)],
            "audit_records": len(self.patch_tool.audit_records),
            "approvals": [record.__dict__ for record in self.approvals.values()],
            "consensus_records": [
                record.__dict__ for record in self.consensus_records.values()],
            "state_root": str(self.state_root),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "organization_id": self.organization.org_id,
            "authority": self.organization.authority,
            "config_version": self.organization.config_version,
            "state_version": self.organization.state_version,
            "resources": [resource.to_dict() for resource in self.registry.list()],
            "tools": [tool.to_dict() for tool in self.tools.list()],
            "reuse": self.reuse_stats(),
            "coordinator": {
                "intents": len(self.coordinator.intents),
                "duplicate_commits": self.coordinator.duplicate_commits,
                "interruptions": self.coordinator.interruptions,
            },
            "decisions": len(self.decision_engine.decisions),
            "executions": len(self.executions),
            "audit_records": len(self.patch_tool.audit_records),
            "contexts": len(self.contexts),
            "costs": self.costs(),
            "memory": self.memory(),
            "events": self.events.stats(),
            "project": (self.project.snapshot.to_dict()
                        if self.project.snapshot is not None else None),
            "persisted": self.persist_state,
            "state_root": str(self.state_root),
        }
