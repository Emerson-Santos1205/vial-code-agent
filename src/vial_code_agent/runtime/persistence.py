"""Persistence (RFC-003 continuity) and serialization helpers."""
from __future__ import annotations

from typing import Any

from ..events import EventStore
from ..persistence import PersistenceError
from ..project import ProjectSnapshot
from ..records import ApprovalRecord, ConsensusRecord


class PersistenceMixin:
    """Persistence and serialization methods for VialRuntime state."""

    def persist(self) -> None:
        if not self.persist_state:
            return
        try:
            records = {
                "organization.json": self._organization_to_dict(),
                "decisions.json": {
                did: self._decision_to_dict(d)
                for did, d in self.decision_engine.decisions.items()},
                "intents.json": {
                op_id: self._intent_to_dict(intent)
                for op_id, intent in self.coordinator.intents.items()},
                "reuse.json": {
                "_stats": {
                    "reuse_hits": self.reuse_engine.reuse_hits,
                    "recomputes": self.reuse_engine.recomputes,
                    "invalidations": self.reuse_engine.invalidations,
                },
                "cache": {
                    sig: self._reuse_to_dict(entry)
                    for sig, entry in self.reuse_engine.cache.items()},
                },
                "audit.json": [record.__dict__ for record in self.patch_tool.audit_records],
                "approvals.json": [record.__dict__ for record in self.approvals.values()],
                "consensus.json": [record.__dict__ for record in self.consensus_records.values()],
                "cost.json": self._costs.to_dict(),
                "executions.json": self.executions,
                "events.json": self.events.to_list(),
                "contexts.json": {
                context_id: self._context_to_dict(context)
                for context_id, context in self.contexts.items()},
            }
            if self.project.snapshot is not None:
                records["project.json"] = self.project.snapshot.to_dict()
            self.repository.save_snapshot(records)
            for name, value in records.items():
                self.repository.save(name, value)
        except Exception as exc:
            raise PersistenceError(
                f"failed to persist VIAL runtime state in {self.state_root}") from exc

    def _load_persisted(self) -> None:
        if not self.persist_state:
            return
        try:
            snapshot = self.repository.load_snapshot()

            def has_record(name: str) -> bool:
                return (name in snapshot if snapshot is not None else
                        (self.state_root / name).is_file())

            def load_record(name: str) -> Any:
                return (snapshot[name] if snapshot is not None
                        else self.repository.load(name))

            if has_record("organization.json"):
                data = load_record("organization.json")
                self.organization.authority = data["authority"]
                self.organization.config_version = data["config_version"]
                self.organization.state_version = data["state_version"]
                self.organization.fields = {
                    key: self._state.StateField(
                        key, field["value"], field["relevance"], field["authority"])
                    for key, field in data["fields"].items()}
                self.organization.transitions = [
                    self._transition_from_dict(t) for t in data["transitions"]]
            if has_record("decisions.json"):
                data = load_record("decisions.json")
                self.decision_engine.decisions = {
                    did: self._decision_from_dict(d) for did, d in data.items()}
            if has_record("intents.json"):
                data = load_record("intents.json")
                self.coordinator.intents = {
                    op_id: self._coordinator.Intent(**intent)
                    for op_id, intent in data.items()}
            if has_record("reuse.json"):
                data = load_record("reuse.json")
                stats = data.get("_stats", {})
                self.reuse_engine.reuse_hits = stats.get("reuse_hits", 0)
                self.reuse_engine.recomputes = stats.get("recomputes", 0)
                self.reuse_engine.invalidations = stats.get("invalidations", 0)
                self.reuse_engine.cache = {
                    sig: self._reuse.CachedResult(**entry)
                    for sig, entry in data.get("cache", data).items()}
            if has_record("audit.json"):
                data = load_record("audit.json")
                self.patch_tool.audit_records = [
                    self._tool.AuditRecord(**record) for record in data]
            if has_record("approvals.json"):
                data = load_record("approvals.json")
                self.approvals = {
                    record["decision_id"]: ApprovalRecord(**record)
                    for record in data}
            if has_record("consensus.json"):
                data = load_record("consensus.json")
                self.consensus_records = {
                    record["decision_id"]: ConsensusRecord(**record)
                    for record in data}
            if has_record("cost.json"):
                data = load_record("cost.json")
                self._costs = self._cost.CostComponents(
                    tokens=data.get("tokens", 0.0),
                    inference=data.get("inference", 0.0),
                    latency=data.get("latency", 0.0),
                    retrieval=data.get("retrieval", 0.0),
                    construction=data.get("construction", 0.0),
                    validation=data.get("validation", 0.0),
                )
            if has_record("executions.json"):
                self.executions = list(load_record("executions.json"))
            if has_record("events.json"):
                self.events = EventStore.from_list(
                    load_record("events.json"))
                self.events.configure({self.actor, self.authority})
            if has_record("project.json"):
                self.project.restore(ProjectSnapshot.from_dict(
                    load_record("project.json")))
                self.project.configure({self.actor, self.authority})
            if has_record("contexts.json"):
                self.contexts = {
                    context_id: self._context_from_dict(data)
                    for context_id, data
                    in load_record("contexts.json").items()}
        except Exception as exc:
            raise PersistenceError(
                f"failed to restore VIAL runtime state from {self.state_root}") from exc

    def _organization_to_dict(self) -> dict[str, Any]:
        return {
            "org_id": self.organization.org_id,
            "authority": self.organization.authority,
            "config_version": self.organization.config_version,
            "state_version": self.organization.state_version,
            "fields": {
                key: {"value": field.value, "relevance": field.relevance,
                      "authority": field.authority}
                for key, field in self.organization.fields.items()},
            "transitions": [self._transition_to_dict(t)
                            for t in self.organization.transitions],
        }

    @staticmethod
    def _transition_to_dict(t: Any) -> dict[str, Any]:
        return {
            "transition_id": t.transition_id,
            "organization": t.organization,
            "previous_version": t.previous_version,
            "resulting_version": t.resulting_version,
            "operation": t.operation,
            "authority": t.authority,
            "provenance": t.provenance,
            "timestamp": t.timestamp,
        }

    def _transition_from_dict(self, d: dict[str, Any]) -> Any:
        return self._state.StateTransition(**d)

    @staticmethod
    def _decision_to_dict(d: Any) -> dict[str, Any]:
        return d.to_dict()

    def _decision_from_dict(self, d: dict[str, Any]) -> Any:
        authority = self._decision.Authority(**d["authority"])
        fields = {k: v for k, v in d.items() if k != "authority"}
        return self._decision.Decision(**fields, authority=authority)

    @staticmethod
    def _context_to_dict(context: Any) -> dict[str, Any]:
        return {
            "context_id": context.context_id,
            "task_id": context.task_id,
            "organization_id": context.organization_id,
            "body": context.body,
            "mode": context.mode,
            "state_version": context.state_version,
            "tokens": context.tokens,
            "references": list(context.references),
            "objective": context.objective,
            "scope": context.scope,
            "status": context.status,
            "version": context.version,
            "created_at": context.created_at,
        }

    def _context_from_dict(self, d: dict[str, Any]) -> Any:
        return self._context.Context(**d)

    @staticmethod
    def _intent_to_dict(intent: Any) -> dict[str, Any]:
        return {
            "operation_id": intent.operation_id,
            "key": intent.key,
            "value": intent.value,
            "actor": intent.actor,
            "previous_version": intent.previous_version,
            "status": intent.status,
            "resulting_version": intent.resulting_version,
            "created_at": intent.created_at,
        }

    @staticmethod
    def _reuse_to_dict(entry: Any) -> dict[str, Any]:
        return {
            "signature": entry.signature,
            "outcome": entry.outcome,
            "quality": entry.quality,
            "state_version": entry.state_version,
            "referenced_fields": entry.referenced_fields,
            "provenance": entry.provenance,
            "created_at": entry.created_at,
        }
