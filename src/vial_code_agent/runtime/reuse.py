"""Cognitive reuse (RFC-008)."""
from __future__ import annotations

from typing import Any


class CognitiveReuseMixin:
    """Cognitive reuse methods for caching and reusing cognition results."""

    def lookup_reuse(self, task: Any) -> tuple[Any | None, str]:
        entry, outcome = self.reuse_engine.lookup(task)  # type: ignore[attr-defined]
        if outcome == "hit":
            self.reuse_engine.reuse_hits += 1  # type: ignore[attr-defined]
        return entry, outcome

    def store_reuse(self, task: Any, outcome: Any, quality: float,
                    context: Any) -> Any:
        self.reuse_engine.recomputes += 1  # type: ignore[attr-defined]
        return self.reuse_engine.store(  # type: ignore[attr-defined]
            task, outcome, quality, context,
            provenance=f"org:{self.org_id}:runtime",  # type: ignore[attr-defined]
        )

    def reuse_stats(self) -> dict[str, Any]:
        return self.reuse_engine.stats()  # type: ignore[attr-defined]
