from __future__ import annotations

import difflib
import functools
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from .evidence import validate_candidate
from .model import HttpModelProvider, ModelResponse, OpenCodeProvider
from .vial_runtime import VialRuntime


def _stream_error(response: ModelResponse | None) -> str:
    """Turn an empty model stream into an actionable UI message."""
    if response is None:
        return "error: model returned an empty response; no process result was available"
    detail = response.stderr.strip() if response.stderr else ""
    if detail:
        return f"error: model exited with code {response.returncode}: {detail}"
    if response.returncode:
        return f"error: model exited with code {response.returncode} without output"
    return "error: model completed successfully but returned an empty response"


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
            model_ref, self.executable, self.auto_approve, self.agent,
            self.timeout_seconds)

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
            emitted = False
            try:
                for chunk in provider.chat_stream(task, root, history=history):
                    emitted = emitted or bool(chunk)
                    yield chunk
            finally:
                self._active_provider = None
            if not emitted:
                response = getattr(provider, "last_response", None)
                yield _stream_error(response)
            return
        decision = self.analyze(task)
        if decision.tier == "deterministic":
            yield f"deterministic: {decision.deterministic_keyword}"
            return
        response, _decision = self.dispatch(task, root, requested_model, history)
        if response.returncode != 0 or not response.text.strip():
            yield _stream_error(response)
        else:
            yield response.text

    def cancel_active(self) -> None:
        """Terminate the model subprocess currently streaming, if any."""
        provider = self._active_provider
        if provider is not None:
            provider.cancel()
            self._active_provider = None

    # ------------------------------------------------------------------ #
    # Cross-model consensus: safety layer independent of ``dispatch``.
    #
    # ``dispatch`` optimizes for cost/latency (cheapest tier, first valid
    # response wins) and never compares model outputs to each other. That is
    # the right default for routine work, but it means "safe" only ever
    # covers *who is allowed to act* (the AuthorizationGate), never *whether
    # the action's content is trustworthy*. For higher-risk tasks, a single
    # model's output should not be the sole basis for an authorized action.
    #
    # ``dispatch_consensus`` asks >=2 independent models for the same task
    # and requires their answers to agree above a threshold before treating
    # the result as usable. Disagreement is not an error to hide or retry
    # silently -- it is signal that a human should look at the task before
    # any Decision derived from it is authorized. Callers are expected to
    # gate ``Decision.approve``/``authorize`` on ``ConsensusResult.agreed``
    # rather than always feeding the winning response straight through.
    # ------------------------------------------------------------------ #
    def dispatch_consensus(
        self,
        task: str,
        root: Path | None = None,
        models: list[str] | None = None,
        quorum: int = 2,
        min_agreement: float = 0.6,
        history: list[tuple[str, str]] | None = None,
        require_evidence: bool = False,
        test_command: list[str] | None = None,
        test_timeout: int = 120,
    ) -> tuple[ConsensusResult, RouteDecision]:
        """Dispatch to >=``quorum`` independent models and require agreement.

        Returns a :class:`ConsensusResult` carrying every raw response (for
        audit/human review) plus the pairwise agreement ratio, and a
        :class:`RouteDecision` describing which models were consulted.
        """
        decision = self.analyze(task)
        candidates = list(models) if models else self.candidates(decision)
        if not candidates and self.default_model and self.default_model != "auto":
            candidates = [self.default_model]
        chosen = candidates[:max(quorum, 2)] if len(candidates) >= 2 else candidates

        responses: dict[str, ModelResponse] = {}
        if chosen:
            providers = [(ref, self._provider_for(ref)) for ref in chosen]
            with ThreadPoolExecutor(max_workers=max(1, len(providers))) as executor:
                futures = {
                    executor.submit(
                        functools.partial(provider.chat, history=history)
                        if history else provider.chat,
                        task, root,
                    ): ref
                    for ref, provider in providers
                }
                for future in as_completed(futures):
                    ref = futures[future]
                    try:
                        responses[ref] = future.result()
                    except (OSError, RuntimeError) as error:
                        responses[ref] = ModelResponse("", 1, stderr=str(error))

        valid = {
            ref: response for ref, response in responses.items()
            if response.returncode == 0 and response.text.strip()
        }
        if len(valid) < 2:
            # Cannot compute agreement with fewer than two valid answers.
            # Surface what happened without ever claiming consensus.
            if valid:
                ref, response = next(iter(valid.items()))
                note = "insufficient candidates for consensus"
            else:
                ref = ""
                response = next(
                    iter(responses.values()),
                    ModelResponse("", 1, stderr="no candidates"))
                note = "all consensus candidates failed"
            return ConsensusResult(False, response, 0.0, responses), RouteDecision(
                tier=decision.tier, candidates=chosen, model=ref, note=note)

        (ref_a, resp_a), (ref_b, resp_b) = max(
            combinations(valid.items(), 2),
            key=lambda pair: _agreement_ratio(pair[0][1], pair[1][1]),
        )
        ratio = _agreement_ratio(resp_a, resp_b)
        evidence: dict[str, dict[str, object]] = {}
        evidence_passed = True
        if require_evidence:
            evidence_passed = False
            for ref, response in valid.items():
                patch = _extract_candidate_patch(response.text)
                result = (validate_candidate(root, patch, test_command, test_timeout)
                          if root is not None and patch else None)
                evidence[ref] = {
                    "static_valid": bool(result and result.static_valid),
                    "behavioral_passed": result.behavioral_passed if result else False,
                    "detail": result.detail if result else "no unified diff candidate",
                }
            evidence_passed = all(
                evidence.get(ref, {}).get("static_valid")
                and evidence.get(ref, {}).get("behavioral_passed") is not False
                for ref in (ref_a, ref_b))
        agreed = ratio >= min_agreement and evidence_passed
        winner_ref, winner_response = (ref_a, resp_a)
        return ConsensusResult(agreed, winner_response, ratio, responses, evidence,
                               evidence_passed), RouteDecision(
            tier=decision.tier, candidates=chosen, model=winner_ref,
            note=f"consensus={agreed} ratio={ratio:.2f}")


def _agreement_ratio(a: ModelResponse, b: ModelResponse) -> float:
    """Semantic similarity between two model responses, in [0, 1].

    Compares at the file/hunk level rather than raw text: two patches that
    change the same files with equivalent hunks score high even if the prose
    around them differs. Falls back to character-level SequenceMatcher when
    neither response contains a unified diff.
    """
    patch_a = _extract_candidate_patch(a.text)
    patch_b = _extract_candidate_patch(b.text)
    if patch_a and patch_b:
        files_a = _parse_diff_files(patch_a)
        files_b = _parse_diff_files(patch_b)
        all_files = sorted(set(files_a) | set(files_b))
        if not all_files:
            return 1.0
        file_scores = []
        for path in all_files:
            hunks_a = files_a.get(path, [])
            hunks_b = files_b.get(path, [])
            if not hunks_a and not hunks_b:
                continue
            if not hunks_a or not hunks_b:
                file_scores.append(0.0)
                continue
            ratio = difflib.SequenceMatcher(
                None, "\n".join(hunks_a), "\n".join(hunks_b)).ratio()
            file_scores.append(ratio)
        return sum(file_scores) / len(file_scores) if file_scores else 1.0
    return difflib.SequenceMatcher(None, a.text, b.text).ratio()


def _parse_diff_files(patch: str) -> dict[str, list[str]]:
    """Extract per-file hunk bodies from a unified diff."""
    files: dict[str, list[str]] = {}
    current_file = None
    current_hunks: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            if current_file and current_hunks:
                files[current_file] = current_hunks
            current_file = line[6:]
            current_hunks = []
        elif line.startswith("@@"):
            current_hunks.append(line)
        elif current_file is not None:
            current_hunks.append(line)
    if current_file and current_hunks:
        files[current_file] = current_hunks
    return files


@dataclass(frozen=True)
class ConsensusResult:
    """Outcome of :meth:`RoutingGraph.dispatch_consensus`.

    ``responses`` keeps every raw, per-model answer (not just the winner) so
    a disagreement can be handed to a human with full context instead of
    only the router's pick.
    """

    agreed: bool
    response: ModelResponse
    agreement_ratio: float
    responses: dict[str, ModelResponse] = field(default_factory=dict)
    evidence: dict[str, dict[str, object]] = field(default_factory=dict)
    evidence_passed: bool = True


def _extract_candidate_patch(text: str) -> str | None:
    start = text.find("diff --git ")
    if start < 0:
        start = text.find("--- ")
    if start < 0:
        return None
    return text[start:].strip() + "\n"


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
