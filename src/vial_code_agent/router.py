from __future__ import annotations


class ModelRouter:
    """Small deterministic routing policy for the first application slice."""

    def route(self, task: str, requested_model: str = "auto") -> str:
        if requested_model != "auto":
            return requested_model
        lowered = task.lower()
        if any(word in lowered for word in ("explain", "document", "rename")):
            return "fast"
        return "reasoning"
