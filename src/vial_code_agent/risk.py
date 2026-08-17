"""Deterministic risk classification for automatic provider approval."""
from __future__ import annotations

from dataclasses import dataclass

RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclass(frozen=True)
class RiskPolicy:
    max_auto_risk: str = "medium"

    def allows_auto(self, risk: str) -> bool:
        return RISK_ORDER.get(risk, 3) <= RISK_ORDER.get(self.max_auto_risk, 1)


def classify_task(task: str) -> str:
    lowered = task.lower()
    if any(word in lowered for word in
           ("credential", "secret", "deploy", "production", "git push", "remote")):
        return "critical"
    if any(word in lowered for word in
           ("dependency", "install", "shell", "command", "migration", "git ")):
        return "high"
    if any(word in lowered for word in ("read", "inspect", "explain", "analyze")):
        return "low"
    return "medium"
