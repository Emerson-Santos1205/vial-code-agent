from __future__ import annotations

import difflib
import functools
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        if deterministic_solvable(task):
            return "deterministic"
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
    TIER_COST = {"light": 0, "advanced": 1}

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
        self._active_provider = None

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
        history: list[tuple[str, str]] | None = None,
    ) -> tuple[ModelResponse, RouteDecision]:
        if requested_model != "auto":
            decision = RouteDecision(
                tier="advanced", candidates=[requested_model], model=requested_model)
            provider = self._provider_for(requested_model)
            response = (
                provider.chat(task, root, history=history)
                if history else provider.chat(task, root))
            return response, decision
        decision = self.analyze(task)
        if decision.tier == "deterministic":
            return ModelResponse(
                f"deterministic: {decision.deterministic_keyword}", 0,
            ), decision
        candidates = self.candidates(decision)
        if not candidates:
            return ModelResponse("", 1, stderr="no models in routing pool"), decision

        # Staged tier escalation (RFC-010 cost order): dispatch the cheapest
        # tier first in parallel; only when a whole tier fails do we escalate
        # to the next, instead of fanning every prompt out to the full pool.
        tiered: dict[str, list[str]] = {}
        for ref in candidates:
            tiered.setdefault(_tier_of(ref), []).append(ref)
        first_error: ModelResponse | None = None
        for tier in sorted(tiered, key=lambda tier: self.TIER_COST[tier]):
            response, ref, error_response = self._dispatch_refs(
                task, root, tiered[tier], history)
            if response is not None:
                return response, RouteDecision(
                    tier=decision.tier, candidates=candidates, model=ref)
            if first_error is None:
                first_error = error_response
        return first_error or ModelResponse(
            "", 1, stderr="all routing candidates failed"), RouteDecision(
            tier=decision.tier, candidates=candidates, model=candidates[0])

    # ------------------------------------------------------------------ #
    # Parallel dispatch within one tier; returns the first valid response.
    # ``error_response`` preserves the tier's first failure so a total
    # failure can be reported without an extra model call.
    # ------------------------------------------------------------------ #
    def _dispatch_refs(
        self,
        task: str,
        root: Path | None,
        refs: list[str],
        history: list[tuple[str, str]] | None,
    ) -> tuple[ModelResponse | None, str, ModelResponse]:
        providers = [
            (ref, self._provider_for(ref)) for ref in refs
        ]
        results: dict[int, tuple[str, ModelResponse]] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(providers))) as executor:
            futures = {
                executor.submit(
                    functools.partial(provider.chat, history=history)
                    if history else provider.chat,
                    task, root,
                ): index
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
                return response, ref, ModelResponse("", 1)
        for index in sorted(results):
            ref, response = results[index]
            if response.text.strip():
                return response, ref, ModelResponse("", 1)
        first_index = min(results) if results else 0
        ref, response = results.get(
            first_index, ("", ModelResponse("", 1, stderr="no candidates")))
        return None, "", response

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

    # ------------------------------------------------------------------ #
    # Streaming dispatch: pinned-model chat yields chunks as they stream.
    # The pool / deterministic paths fall back to the blocking dispatch and
    # yield the whole text at once (deterministic has no model call).
    # ------------------------------------------------------------------ #
    def dispatch_stream(
        self,
        task: str,
        root: Path | None = None,
        requested_model: str = "auto",
        history: list[tuple[str, str]] | None = None,
    ):
        if requested_model != "auto":
            provider = self._provider_for(requested_model)
            self._active_provider = provider
            try:
                for chunk in provider.chat_stream(task, root, history=history):
                    yield chunk
            finally:
                self._active_provider = None
            return
        decision = self.analyze(task)
        if decision.tier == "deterministic":
            yield f"deterministic: {decision.deterministic_keyword}"
            return
        response, _decision = self.dispatch(task, root, requested_model, history)
        yield response.text

    def cancel_active(self) -> None:
        """Terminate the model subprocess currently streaming, if any."""
        provider = self._active_provider
        if provider is not None:
            provider.cancel()
            self._active_provider = None


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
