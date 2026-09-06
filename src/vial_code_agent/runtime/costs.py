"""Cost-aware, Deterministic-First routing and economic cost accounting.

RFC-004, RFC-010, RFC-004 §21-23.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RESOURCE_TIERS = {"deterministic": 0, "light": 0.5, "advanced": 1.0}
RESOURCE_ORDER = ["deterministic", "light", "advanced"]
TIER_MODEL = {"deterministic": "", "light": "fast", "advanced": "reasoning"}


@dataclass
class CostAccumulator:
    tokens: float = 0.0
    inference: float = 0.0
    latency: float = 0.0
    retrieval: float = 0.0
    construction: float = 0.0
    validation: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {
            "tokens": self.tokens,
            "inference": self.inference,
            "latency": self.latency,
            "retrieval": self.retrieval,
            "construction": self.construction,
            "validation": self.validation,
        }


class CostAccountingMixin:
    """Cost-aware routing and economic cost accounting methods."""

    def capable_tiers(self) -> list[str]:
        tiers = set()
        for resource in self.registry.list():  # type: ignore[attr-defined]
            for capability in resource.capabilities.values():
                tier = capability.constraints.get("tier")
                if tier:
                    tiers.add(tier)
        return [tier for tier in RESOURCE_ORDER if tier in tiers]

    def select_route(self, task_text: str, requested_model: str = "auto",
                     deterministic: bool = False) -> str | None:
        if requested_model != "auto":
            return requested_model
        tiers = self.capable_tiers()
        if not deterministic:
            tiers = [tier for tier in tiers if tier != "deterministic"]
        try:
            tier = self.selector.select(deterministic, tiers)  # type: ignore[attr-defined]
        except Exception:
            tier = "advanced"
        return TIER_MODEL.get(tier, "reasoning")

    def record_inference(self, input_tokens: int, output_tokens: int,
                         tier: str = "advanced") -> None:
        multiplier = RESOURCE_TIERS.get(tier, RESOURCE_TIERS["advanced"])
        self._accumulate(self.cost_model.infer(  # type: ignore[attr-defined]
            input_tokens or 0, output_tokens or 0, tier_multiplier=multiplier))

    def record_retrieval(self, n_ops: int = 1) -> None:
        self._accumulate(self.cost_model.retrieval(n_ops))  # type: ignore[attr-defined]

    def record_construction(self, n_contexts: int = 1) -> None:
        self._accumulate(self.cost_model.construction(n_contexts))  # type: ignore[attr-defined]

    def record_validation(self, n_validations: int = 1) -> None:
        self._accumulate(self.cost_model.validation(n_validations))  # type: ignore[attr-defined]

    def _accumulate(self, components: Any) -> None:
        self._costs.tokens += components.tokens  # type: ignore[attr-defined]
        self._costs.inference += components.inference  # type: ignore[attr-defined]
        self._costs.latency += components.latency  # type: ignore[attr-defined]
        self._costs.retrieval += components.retrieval  # type: ignore[attr-defined]
        self._costs.construction += components.construction  # type: ignore[attr-defined]
        self._costs.validation += components.validation  # type: ignore[attr-defined]

    def costs(self) -> dict[str, float]:
        return self._costs.to_dict()  # type: ignore[attr-defined]
