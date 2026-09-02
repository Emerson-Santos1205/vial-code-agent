"""Typed structures for SWE-bench candidate and consensus pipelines."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PipelineStages:
    """Per-candidate pipeline stage verdicts."""
    patch: str = "FAIL"
    static: str = "FAIL"
    behavioral: str = "NOT_RUN"
    result: str = ""


@dataclass
class CandidateOutcome:
    """Observable pipeline result for one independent candidate."""
    candidate_id: str = ""
    model: str = ""
    pipeline: PipelineStages = field(default_factory=PipelineStages)
    returned_patch: bool = False
    patch_returns: int = 0
    attempts: int = 1
    retries: int = 0
    patch_valid: bool = False
    tests_passed: bool | None = None
    result_code: str = ""
    failure_detail: str = ""
    # Optional enrichment fields
    response_received: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    context_tokens: int = 0
    provider_stderr: str = ""
    prompt_sha256: str = ""
    protocol: dict[str, str] = field(default_factory=dict)
    protocol_sha256: str = ""
    workspace_sha256: str = ""
    failure_stage: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "model": self.model,
            "pipeline": {
                "patch": self.pipeline.patch,
                "static": self.pipeline.static,
                "behavioral": self.pipeline.behavioral,
                "result": self.pipeline.result,
            },
            "returned_patch": self.returned_patch,
            "patch_returns": self.patch_returns,
            "attempts": self.attempts,
            "retries": self.retries,
            "patch_valid": self.patch_valid,
            "tests_passed": self.tests_passed,
            "result_code": self.result_code,
            "failure_detail": self.failure_detail,
        }
        if self.response_received:
            d["response_received"] = True
        if self.input_tokens:
            d["input_tokens"] = self.input_tokens
        if self.output_tokens:
            d["output_tokens"] = self.output_tokens
        if self.context_tokens:
            d["context_tokens"] = self.context_tokens
        if self.provider_stderr:
            d["provider_stderr"] = self.provider_stderr
        if self.prompt_sha256:
            d["prompt_sha256"] = self.prompt_sha256
        if self.protocol:
            d["protocol"] = dict(self.protocol)
        if self.protocol_sha256:
            d["protocol_sha256"] = self.protocol_sha256
        if self.workspace_sha256:
            d["workspace_sha256"] = self.workspace_sha256
        if self.failure_stage:
            d["failure_stage"] = self.failure_stage
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateOutcome:
        pipeline = data.get("pipeline", {})
        if isinstance(pipeline, PipelineStages):
            ps = pipeline
        else:
            ps = PipelineStages(
                patch=str(pipeline.get("patch", "FAIL")),
                static=str(pipeline.get("static", "FAIL")),
                behavioral=str(pipeline.get("behavioral", "NOT_RUN")),
                result=str(pipeline.get("result", "")),
            )
        return cls(
            candidate_id=str(data.get("candidate_id", "")),
            model=str(data.get("model", "")),
            pipeline=ps,
            returned_patch=bool(data.get("returned_patch", False)),
            patch_returns=int(data.get("patch_returns", 0)),
            attempts=int(data.get("attempts", 1) or 1),
            retries=int(data.get("retries", 0) or 0),
            patch_valid=bool(data.get("patch_valid", False)),
            tests_passed=data.get("tests_passed"),
            result_code=str(data.get("result_code", "")),
            failure_detail=str(data.get("failure_detail", "")),
            response_received=bool(data.get("response_received", False)),
            input_tokens=int(data.get("input_tokens", 0) or 0),
            output_tokens=int(data.get("output_tokens", 0) or 0),
            context_tokens=int(data.get("context_tokens", 0) or 0),
            provider_stderr=str(data.get("provider_stderr", "")),
            prompt_sha256=str(data.get("prompt_sha256", "")),
            protocol=dict(data.get("protocol", {})),
            protocol_sha256=str(data.get("protocol_sha256", "")),
            workspace_sha256=str(data.get("workspace_sha256", "")),
            failure_stage=str(data.get("failure_stage", "")),
        )


@dataclass
class CandidateConsensus:
    """Auditable consensus evidence from two validated patches."""
    agreed: bool = False
    agreement_ratio: float = 0.0
    models: list[str] = field(default_factory=list)
    responses: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    candidate_outcomes: dict[str, Any] = field(default_factory=dict)
    status: str = ""
    result_code: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agreed": self.agreed,
            "agreement_ratio": self.agreement_ratio,
            "models": list(self.models),
            "responses": dict(self.responses),
            "evidence": dict(self.evidence),
            "candidate_outcomes": dict(self.candidate_outcomes),
            "status": self.status,
            "result_code": self.result_code,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateConsensus:
        return cls(
            agreed=bool(data.get("agreed", False)),
            agreement_ratio=float(data.get("agreement_ratio", 0.0)),
            models=list(data.get("models", [])),
            responses=dict(data.get("responses", {})),
            evidence=dict(data.get("evidence", {})),
            candidate_outcomes=dict(data.get("candidate_outcomes", {})),
            status=str(data.get("status", "")),
            result_code=str(data.get("result_code", "")),
            note=str(data.get("note", "")),
        )


@dataclass
class CandidateResult:
    """Complete generation and validation result for one candidate."""
    model: str = ""
    patch: str | None = None
    generated: Any = None  # SimpleNamespace or CodeAgent response
    outcome: CandidateOutcome = field(default_factory=CandidateOutcome)
    behavior: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "patch": self.patch,
            "outcome": self.outcome.to_dict(),
            "behavior": self.behavior,
        }
