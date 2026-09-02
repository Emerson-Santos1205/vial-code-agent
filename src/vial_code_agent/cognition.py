"""Cognition Engine boundary for the VIAL code agent (RUNTIME-006).

A Cognition Engine transforms an explicit Context into structured reasoning and
produces a Decision Proposal for evaluation. It MUST NOT authorize or execute
anything: the Decision Engine (SDK-005) evaluates, the AuthorizationGate
authorizes, and a Tool executes (RUNTIME-006 §8, §73).

The engine follows RUNTIME-006 conformance requirements:
1. operates on explicit Context;
2. respects Objective and Constraints;
3. distinguishes Cognition from Execution;
4. preserves Decision provenance (evidence);
5. distinguishes fact from inference;
6. represents meaningful uncertainty (confidence);
7. respects authority (required_authority);
8. supports validation;
9. avoids unnecessary cognitive cost (Deterministic First, RFC-010);
10. supports traceability of consequential Decisions (cognitive trace).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import extract_diff
from .router import deterministic_solvable, resolve_deterministic


@dataclass(frozen=True)
class CognitionRequest:
    """Structured input to a Cognition cycle (RUNTIME-006 §83)."""
    cycle: str
    objective: str
    context: Any
    constraints: list[str] = field(default_factory=list)
    policies: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    authority: str = ""
    requested_model: str = "auto"
    root: Path | None = None
    files: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class CognitionResult:
    """Structured output of a Cognition cycle (RUNTIME-006 §83)."""
    cycle: str
    decision_proposal: str | None
    alternatives: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    rationale: str = ""
    confidence: float = 0.0
    risks: list[str] = field(default_factory=list)
    required_authority: str = ""
    model: str = ""
    deterministic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle,
            "decision_proposal": self.decision_proposal,
            "alternatives": self.alternatives,
            "evidence": self.evidence,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "risks": self.risks,
            "required_authority": self.required_authority,
            "model": self.model,
            "deterministic": self.deterministic,
        }


class CognitionEngine:
    """Transforms a frozen Context into a structured Decision Proposal.

    Deterministic-First (RFC-010 §2.4): mechanical tasks resolve without any
    model invocation; everything else routes to the configured provider. The
    engine never authorizes; callers must propose a Decision and pass it
    through Authorization before any Tool executes.
    """

    def __init__(self, provider: Any | None = None) -> None:
        self.provider = provider

    def route(self, objective: str, requested_model: str = "auto") -> str:
        if requested_model != "auto":
            return requested_model
        return "deterministic" if deterministic_solvable(objective) else "model"

    def evaluate(self, request: CognitionRequest) -> CognitionResult:
        route = self.route(request.objective, request.requested_model)
        cycle = request.cycle
        context_tokens = getattr(request.context, "tokens", 0)

        if route == "deterministic" or deterministic_solvable(request.objective):
            patch = None
            if request.root is not None and request.files:
                patch = resolve_deterministic(
                    request.objective, request.root, request.files)
            return CognitionResult(
                cycle=cycle,
                decision_proposal=patch if patch is not None else "no-op",
                alternatives=["model inference (avoided)"],
                evidence=[f"context:{getattr(request.context, 'context_id', '')}",
                          "deterministic:mechanical-transform"],
                rationale=(
                    "task is mechanically solvable; resolved without a model "
                    "call (RFC-010 Deterministic First)"),
                confidence=1.0,
                risks=["low"],
                required_authority=request.authority or "org-root",
                model="deterministic",
                deterministic=True,
            )

        if self.provider is None:
            return CognitionResult(
                cycle=cycle, decision_proposal=None,
                evidence=[f"context:{getattr(request.context, 'context_id', '')}"],
                rationale="no cognitive resource is configured",
                confidence=0.0, risks=["unavailable"],
                required_authority=request.authority or "org-root",
                model="unavailable",
            )

        response = self.provider.generate(
            request.objective, directory=request.root, files=request.files)
        patch = extract_diff(response.text) if response.returncode == 0 else None
        confidence = 1.0 if patch is not None else 0.0
        return CognitionResult(
            cycle=cycle,
            decision_proposal=patch,
            alternatives=[],
            evidence=[
                f"context:{getattr(request.context, 'context_id', '')}",
                f"tokens:{context_tokens}",
                f"model_tokens_in:{response.input_tokens or 0}",
                f"model_tokens_out:{response.output_tokens or 0}",
            ],
            rationale=(
                "model cognition over the selective Context; proposal still "
                "requires Decision + Authorization before execution"),
            confidence=confidence,
            risks=["medium" if patch is None else "low"],
            required_authority=request.authority or "org-root",
            model=request.requested_model if request.requested_model != "auto" else "model",
            deterministic=False,
        )

    @staticmethod
    def propose(runtime: Any, request: CognitionRequest,
                result: CognitionResult) -> Any:
        """Hand the Cognition output to the Decision Engine (SDK-005).

        Returns an AUTHORIZED Decision (approve + authorize by the runtime
        root) so that an AuthorizationGate-bound Tool can execute it.
        """
        type_name = "deterministic_apply" if result.deterministic else "code_generation"
        policy = "inspect" if result.decision_proposal is None else "development"
        return runtime.propose_decision(
            objective=request.objective, type=type_name, policy=policy,
            context_id=getattr(request.context, "context_id", ""),
            risk="low" if result.deterministic else "medium",
            rationale=result.rationale,
            evidence=result.evidence,
            confidence=result.confidence,
        )
