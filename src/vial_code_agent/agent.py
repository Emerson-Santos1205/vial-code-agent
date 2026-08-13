from __future__ import annotations

from dataclasses import dataclass
import difflib
import hashlib
from pathlib import Path

from .cognition import CognitionEngine, CognitionRequest, CognitionResult
from .model import ModelResponse, OpenCodeProvider, extract_diff
from .core import VialCoreReference
from .router import deterministic_solvable, resolve_deterministic


@dataclass(frozen=True)
class GenerationResult:
    response: ModelResponse
    patch: str | None
    workspace_changed: bool = False
    context_id: str = ""
    route: str = ""
    reused: bool = False
    quality: float = 1.0
    reuse_outcome: str = "n/a"
    tokens: int = 0


def build_prompt(task: str, root: Path, files: list[Path], max_chars: int = 6_000) -> str:
    """Build a bounded, deterministic prompt from selected workspace files."""
    sections: list[str] = [f"Task: {task}\n\nThe file content is:\n"]
    used = sum(len(section) for section in sections)
    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        section = f"\n{relative}:\n{content}\n"
        if used + len(section) > max_chars:
            sections.append("\n[Context truncated at the configured limit]")
            break
        sections.append(section)
        used += len(section)
    sections.append(
        "\nReturn only the complete unified diff for this change."
    )
    return "".join(sections)


class CodeAgent:
    """Orchestrates code generation on top of the VIAL runtime.

    With a runtime attached, the agent follows the full VIAL pipeline:
    selective Context -> cognitive reuse lookup (RFC-008) -> Deterministic
    First routing (RFC-010) -> authorized patch decision. Without a runtime
    it falls back to the plain opencode adapter for compatibility.
    """

    def __init__(self, provider: OpenCodeProvider | None, runtime=None) -> None:
        self.provider = provider
        self.runtime = runtime
        self.cognition = CognitionEngine(provider)

    def plan_cognition(self, task: str, root: Path, files: list[Path],
                       context=None, requested_model: str = "auto") -> CognitionResult:
        """Run a Cognition cycle over the official Context (RUNTIME-006).

        Consumes the selective Context produced by the runtime and returns a
        structured Decision Proposal. No authorization or execution happens
        here; use ``cognition.propose`` + ``runtime.invoke_tool`` downstream.
        """
        cycle = f"CG-{hashlib.sha256(task.encode('utf-8')).hexdigest()[:12]}"
        request = CognitionRequest(
            cycle=cycle, objective=task, context=context,
            authority=getattr(self.runtime, "authority", "org-root") if self.runtime else "org-root",
            requested_model=requested_model,
            root=root, files=files,
            capabilities=["code_transform", "cognition"],
        )
        result = self.cognition.evaluate(request)
        if self.runtime is not None and not result.deterministic:
            self.runtime.record_inference(
                context.tokens if context is not None else 0, 0, tier="advanced")
        return result

    def generate(
        self, task: str, root: Path, files: list[Path], max_chars: int = 6_000,
        vial: VialCoreReference | None = None, runtime=None, max_tokens: int | None = None,
    ) -> GenerationResult:
        before = {path: path.read_bytes() for path in files if path.is_file()}
        context_id = ""
        route = ""
        reused = False
        reuse_outcome = "n/a"
        quality = 1.0
        token_usage = 0
        task_obj = None
        ctx = None
        runtime = runtime or self.runtime

        if runtime is not None:
            runtime.add_workspace_fields(root, files)
            det = deterministic_solvable(task)
            route = runtime.select_route(task, "auto", deterministic=det)
            task_obj = runtime.build_task(
                task, files, root,
                op="code_transform" if det else "code_generation",
            )
            entry, reuse_outcome = runtime.lookup_reuse(task_obj)
            ctx = runtime.build_context(task, root, files)
            context_id = ctx.context_id
            token_usage = ctx.tokens
            runtime.record_retrieval(1)
            if entry is not None:
                runtime.record_construction(1)
                reused = True
                return GenerationResult(
                    response=ModelResponse("reused validated cognition", 0),
                    patch=entry.outcome,
                    context_id=context_id,
                    route="reuse",
                    reused=True,
                    quality=entry.quality,
                    reuse_outcome=reuse_outcome,
                    tokens=token_usage,
                )
            runtime.record_construction(1)
            if det and route is None:
                patch = resolve_deterministic(task, root, files)
                if patch is not None:
                    record = runtime.record_deterministic(task_obj, ctx, patch)
                    quality = record["quality"]
                    runtime.store_reuse(task_obj, patch, quality, ctx)
                    return GenerationResult(
                        response=ModelResponse("deterministic code transform", 0),
                        patch=patch,
                        context_id=context_id,
                        route="deterministic",
                        quality=quality,
                        reuse_outcome=reuse_outcome,
                        tokens=token_usage,
                    )
                # Deterministic task but nothing to change (e.g. already
                # applied): do not invoke a model for a mechanical no-op.
                runtime.record_validation(1)
                return GenerationResult(
                    response=ModelResponse(
                        "deterministic no-op: nothing to change", 0),
                    patch=None,
                    context_id=context_id,
                    route="deterministic",
                    quality=1.0,
                    reuse_outcome=reuse_outcome,
                    tokens=token_usage,
                )
        else:
            if vial is not None and vial.exists():
                context = vial.build_context(task, root, files)
                context_id = context.context_id
                context.consume()

        prompt = task
        response = self.provider.generate(prompt, directory=root, files=files)
        if runtime is not None and task_obj is not None:
            runtime.record_inference(
                response.input_tokens or 0, response.output_tokens or 0, tier=route)
            runtime.record_validation(1)
        patch = extract_diff(response.text)
        if patch is not None:
            if runtime is not None and task_obj is not None and ctx is not None:
                runtime.store_reuse(task_obj, patch, 1.0, ctx)
            return GenerationResult(
                response=response, patch=patch, context_id=context_id,
                route=route or "auto", reused=False, reuse_outcome=reuse_outcome,
                tokens=token_usage,
            )
        for path, original in before.items():
            if not path.is_file():
                continue
            current = path.read_bytes()
            if current == original:
                continue
            try:
                old_text = original.decode("utf-8").splitlines(keepends=True)
                new_text = current.decode("utf-8").splitlines(keepends=True)
            except UnicodeDecodeError:
                continue
            relative = path.relative_to(root).as_posix()
            generated = difflib.unified_diff(
                old_text, new_text, fromfile=f"a/{relative}", tofile=f"b/{relative}"
            )
            return GenerationResult(
                response=response, patch="".join(generated), workspace_changed=True,
                context_id=context_id, route=route or "auto", reuse_outcome=reuse_outcome,
                tokens=token_usage,
            )
        return GenerationResult(
            response=response, patch=None, context_id=context_id,
            route=route or "auto", reuse_outcome=reuse_outcome, tokens=token_usage,
        )
