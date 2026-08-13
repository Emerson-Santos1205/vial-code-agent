from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from pathlib import Path

from .model import HttpModelProvider, ModelResponse, OpenCodeProvider
from .vial_runtime import VialRuntime


class ModelRouter:
    """Small deterministic routing policy for the first application slice."""

    # Roteia deterministicamente cada tarefa para o modelo solicitado ou mais adequado.
    def route(self, task: str, requested_model: str = "auto") -> str:
        if requested_model != "auto":
            return requested_model
        lowered = task.lower()
        if any(word in lowered for word in ("explain", "document", "rename")):
            return "fast"
        return "reasoning"


class VialRouter:
    """Cost-aware, Deterministic-First routing (RFC-004 §23, RFC-010 §2.4).

    Delegates to the official ``ResourceSelector``: a task that is
    deterministically solvable is routed to the deterministic tier (no model
    call); otherwise the cheapest capable model tier is selected.
    """

    def __init__(self, runtime: VialRuntime) -> None:
        self.runtime = runtime

    def route(self, task: str, requested_model: str = "auto") -> str | None:
        if requested_model != "auto":
            return requested_model
        return self.runtime.select_route(
            task, "auto", deterministic=deterministic_solvable(task))


# --------------------------------------------------------------------------- #
# Routing graph over the model pool (chat runtime).
#
# Mirrors the VIAL Deterministic-First chain (RFC-010 §2.4): analyze the
# prompt, pick the cheapest capable tier, then dispatch the prompt to the
# candidate models for that tier. Candidates run in parallel and the first
# valid response wins; priority is deterministic (pool order, cheapest first).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RouteDecision:
    tier: str  # "deterministic" | "light" | "advanced"
    candidates: list[str] = field(default_factory=list)
    deterministic_keyword: str = ""
    model: str = ""
    note: str = ""


class RoutingGraph:
    """Prompt-analysis router over a pool of models (and HTTP servers).

    ``analyze`` decides the tier from the task text alone; ``dispatch`` runs
    the prompt against the tier's candidate models concurrently and returns
    the first valid response, keeping the pool order deterministic.
    """

    LIGHT_WORDS = ("explain", "document", "rename", "summarize", "list", "what")
    ADVANCED_WORDS = (
        "implement", "create", "write", "fix", "refactor", "debug",
        "test", "migrate", "design", "optimize", "review", "generate",
    )
    POOL_MODEL = {"light": "fast", "advanced": "reasoning"}

    def __init__(
        self,
        registry=None,
        default_model: str = "auto",
        executable: str = "opencode",
        auto_approve: bool = False,
        agent: str = "plan",
        timeout_seconds: int = 180,
    ) -> None:
        self.registry = registry
        self.default_model = default_model
        self.executable = executable
        self.auto_approve = auto_approve
        self.agent = agent
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------ #
    # Prompt analysis -> tier decision
    # ------------------------------------------------------------------ #
    def analyze(self, task: str) -> RouteDecision:
        lowered = task.lower()
        for keyword in MECHANICAL_OPS:
            if keyword in lowered:
                return RouteDecision(
                    tier="deterministic", deterministic_keyword=keyword)
        if any(word in lowered for word in self.LIGHT_WORDS):
            return RouteDecision(tier="light")
        if any(word in lowered for word in self.ADVANCED_WORDS):
            return RouteDecision(tier="advanced")
        words = len(lowered.split())
        if words <= 4:
            return RouteDecision(tier="light")
        return RouteDecision(tier="advanced")

    # ------------------------------------------------------------------ #
    # Candidate selection over the pool
    # ------------------------------------------------------------------ #
    def candidates(self, decision: RouteDecision) -> list[str]:
        """Ordered candidate model refs for a decision, cheapest tier first."""
        pool = list(self.registry.pool) if self.registry is not None else []
        if not pool:
            if self.default_model and self.default_model != "auto":
                return [self.default_model]
            return []
        if decision.tier == "light":
            light = [ref for ref in pool if _tier_of(ref) == "light"]
            return light or pool
        return pool

    def model_for(self, task: str, requested_model: str = "auto") -> str:
        if requested_model != "auto":
            return requested_model
        decision = self.analyze(task)
        if decision.tier == "deterministic":
            return ""
        candidates = self.candidates(decision)
        return candidates[0] if candidates else self.default_model

    # ------------------------------------------------------------------ #
    # Parallel dispatch: first valid response, deterministic priority
    # ------------------------------------------------------------------ #
    def dispatch(
        self,
        task: str,
        root: Path | None = None,
        requested_model: str = "auto",
    ) -> tuple[ModelResponse, RouteDecision]:
        if requested_model != "auto":
            decision = RouteDecision(
                tier="advanced", candidates=[requested_model], model=requested_model)
            response = self._provider_for(requested_model).chat(task, root)
            return response, decision
        decision = self.analyze(task)
        if decision.tier == "deterministic":
            return ModelResponse(
                f"deterministic: {decision.deterministic_keyword}", 0,
            ), decision
        candidates = self.candidates(decision)
        if not candidates:
            return ModelResponse("", 1, stderr="no models in routing pool"), decision
        from concurrent.futures import ThreadPoolExecutor, as_completed

        providers = [
            (ref, self._provider_for(ref)) for ref in candidates
        ]
        results: dict[int, tuple[str, ModelResponse]] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(providers))) as executor:
            futures = {
                executor.submit(provider.chat, task, root): index
                for index, (_, provider) in enumerate(providers)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = (
                        providers[index][0], future.result())
                except (OSError, RuntimeError) as error:
                    results[index] = (
                        providers[index][0],
                        ModelResponse("", 1, stderr=str(error)))
        for index in sorted(results):
            ref, response = results[index]
            if response.returncode == 0 and response.text.strip():
                return response, RouteDecision(
                    tier=decision.tier, candidates=candidates, model=ref)
        for index in sorted(results):
            ref, response = results[index]
            if response.text.strip():
                return response, RouteDecision(
                    tier=decision.tier, candidates=candidates, model=ref)
        return results[0][1], RouteDecision(
            tier=decision.tier, candidates=candidates, model=results[0][0])

    # ------------------------------------------------------------------ #
    def _provider_for(self, model_ref: str):
        if self.registry is not None and self.registry.provider_kind(model_ref) == "http":
            server_name, model = self.registry.server_and_model(model_ref)
            server = self.registry.servers[server_name]
            api_key = os.environ.get(server.api_key_env, "") if server.api_key_env else ""
            return HttpModelProvider(
                server.base_url, api_key, model, self.timeout_seconds)
        return OpenCodeProvider(
            model_ref, self.executable, self.auto_approve, self.agent)


def _tier_of(model_ref: str) -> str:
    lowered = model_ref.lower()
    if any(word in lowered for word in ("fast", "mini", "small", "flash")):
        return "light"
    return "advanced"


# --------------------------------------------------------------------------- #
# Deterministic mechanical code transforms (RFC-010 Deterministic First).
# Each keyword maps to a reproducible, model-free source transformation.
# --------------------------------------------------------------------------- #
def deterministic_solvable(task_text: str) -> bool:
    """True when the task matches a registered mechanical transform."""
    lowered = task_text.lower()
    return any(keyword in lowered for keyword in MECHANICAL_OPS)


def resolve_deterministic(task_text: str, root: Path, files: list[Path]) -> str | None:
    """Return a unified diff produced without a model, or None if not applicable."""
    lowered = task_text.lower()
    for keyword, transform in MECHANICAL_OPS.items():
        if keyword in lowered:
            return transform(root, files)
    return None


def _transform_files(root: Path, files: list[Path],
                     transform) -> str | None:
    patches: list[str] = []
    for path in files:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new_text = transform(text)
        if new_text is None or new_text == text:
            continue
        relative = path.relative_to(root).as_posix()
        diff = difflib.unified_diff(
            text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        )
        patches.append("".join(diff))
    if not patches:
        return None
    return "".join(patches)


def _trim_trailing_whitespace(root: Path, files: list[Path]) -> str | None:
    def transform(text: str) -> str:
        return "\n".join(line.rstrip() for line in text.split("\n"))

    return _transform_files(root, files, transform)


def _add_encoding_header(root: Path, files: list[Path]) -> str | None:
    def transform(text: str) -> str | None:
        first_line = text.split("\n", 1)[0]
        if first_line.startswith("# -*- coding:"):
            return None
        return "# -*- coding: utf-8 -*-\n" + text

    return _transform_files(root, files, transform)


# keyword -> transform function; ordered, first match wins.
MECHANICAL_OPS = {
    "trim trailing whitespace": _trim_trailing_whitespace,
    "strip trailing whitespace": _trim_trailing_whitespace,
    "add encoding header": _add_encoding_header,
    "add utf-8 header": _add_encoding_header,
}
