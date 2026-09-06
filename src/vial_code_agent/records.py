"""Shared dataclass records used across runtime modules."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ApprovalRecord:
    """A recorded human/administrative approval (SDK-005, RUNTIME-006 §8)."""
    decision_id: str
    approver: str
    note: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class ConsensusRecord:
    """A recorded cross-model consensus outcome for a Decision."""
    decision_id: str
    agreed: bool
    agreement_ratio: float = 0.0
    models: list[str] = field(default_factory=list)
    responses: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, dict[str, object]] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    note: str = ""
