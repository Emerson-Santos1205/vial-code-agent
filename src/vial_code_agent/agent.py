from __future__ import annotations

from dataclasses import dataclass
import difflib
import hashlib
import shutil
import tempfile
from pathlib import Path

from .cognition import CognitionEngine, CognitionRequest, CognitionResult
from .model import ModelResponse, OpenCodeProvider, extract_diff
from .patches import PatchApplier, PatchError
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
    attempts: int = 1
    failure_type: str = ""


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
        det = deterministic_solvable(task)

        if runtime is not None:
            runtime.add_workspace_fields(root, files)
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
                    runtime.record_validation(1)
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
            if det:
                # Deterministic-First even without a VIAL runtime (RFC-010):
                # resolve the mechanical transform and never invoke a model.
                patch = resolve_deterministic(task, root, files)
                if patch is not None:
                    return GenerationResult(
                        response=ModelResponse("deterministic code transform", 0),
                        patch=patch, route="deterministic", quality=1.0)
                return GenerationResult(
                    response=ModelResponse(
                        "deterministic no-op: nothing to change", 0),
                    patch=None, route="deterministic", quality=1.0)
            if vial is not None and vial.exists():
                context = vial.build_context(task, root, files)
                context_id = context.context_id
                context.consume()

        prompt = (
            f"{task}\n\n"
            "Read the exact contents of the provided files before deciding the fix. "
            "Do not guess line numbers or code. Do not edit files directly. "
            "Return one applicable unified diff with exact removed and added lines."
        )
        # Keep the operator workspace read-only from the provider's perspective.
        with tempfile.TemporaryDirectory(prefix="vial-provider-") as directory:
            staging = Path(directory)
            staged_files: list[Path] = []
            for path in files:
                relative = path.relative_to(root)
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if path.is_file():
                    shutil.copy2(path, target)
                    staged_files.append(target)
            response = self.provider.generate(
                prompt, directory=staging, files=staged_files)
            patch = extract_diff(response.text)
            attempts = 1
            validation_error = ""
            if patch is not None:
                patch = self._normalize_staged_paths(patch, staging, staged_files)
                if patch is None:
                    validation_error = "candidate path does not uniquely match staged files"
                else:
                    try:
                        PatchApplier(staging).validate(patch)
                    except PatchError as error:
                        validation_error = str(error)
                        repaired = PatchApplier(staging).repair_candidate(patch)
                        if repaired:
                            try:
                                PatchApplier(staging).validate(repaired)
                                patch = repaired
                            except PatchError:
                                patch = None
                        else:
                            patch = None
            if patch is None:
                # One bounded contract-recovery attempt. It uses the same
                # staging and provider path; it never authorizes a fallback.
                attempts = 2
                response = self.provider.generate(
                    f"{task}\n\nReturn ONLY a unified diff. Do not explain. "
                    f"The previous candidate was rejected: {validation_error or 'no parseable patch'}. "
                    "Re-open and read the exact current staged file before responding. "
                    "Use exact removed and added lines and return an applicable diff. "
                    "Do not return a no-op where removed and added lines are identical. "
                    "If the requested fix is already present, return a clear explanation "
                    "instead of fabricating a patch.",
                    directory=staging, files=staged_files)
                patch = extract_diff(response.text)
                if patch is not None:
                    patch = self._normalize_staged_paths(patch, staging, staged_files)
                    if patch is not None:
                        try:
                            PatchApplier(staging).validate(patch)
                        except PatchError as error:
                            validation_error = str(error)
                            repaired = PatchApplier(staging).repair_candidate(patch)
                            if repaired:
                                try:
                                    PatchApplier(staging).validate(repaired)
                                    patch = repaired
                                except PatchError:
                                    patch = None
                            else:
                                patch = None
                if patch is None:
                    attempts = 3
                    response = self.provider.generate(
                        f"{task}\n\nFINAL PATCH RECOVERY. Return ONLY a valid unified diff "
                        "starting with --- and +++. Do not include prose, Markdown, "
                        "comments, or apply_patch markers. The diff must apply to "
                        "the exact current staged file after re-reading it. "
                        "Previous validation error: "
                        f"{validation_error}",
                        directory=staging, files=staged_files)
                    patch = extract_diff(response.text)
                    if patch is not None:
                        patch = self._normalize_staged_paths(patch, staging, staged_files)
                        if patch is not None:
                            try:
                                PatchApplier(staging).validate(patch)
                            except PatchError as error:
                                validation_error = str(error)
                                repaired = PatchApplier(staging).repair_candidate(patch)
                                if repaired:
                                    try:
                                        PatchApplier(staging).validate(repaired)
                                        patch = repaired
                                    except PatchError:
                                        patch = None
                                else:
                                    patch = None
        if runtime is not None and task_obj is not None:
            runtime.record_inference(
                response.input_tokens or 0, response.output_tokens or 0, tier=route)
            runtime.record_validation(1)
        if patch is not None:
            if runtime is not None and task_obj is not None and ctx is not None:
                runtime.store_reuse(task_obj, patch, 1.0, ctx)
            return GenerationResult(
                response=response, patch=patch, context_id=context_id,
                route=route or "auto", reused=False, reuse_outcome=reuse_outcome,
                tokens=token_usage,
                attempts=attempts,
                failure_type="" if patch else f"PATCH_CONTRACT: {validation_error}",
                workspace_changed=self._workspace_changed(before),
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
                tokens=token_usage, attempts=attempts,
                failure_type=f"PATCH_CONTRACT: {validation_error}",
            )
        return GenerationResult(
            response=response, patch=None, context_id=context_id,
            route=route or "auto", reuse_outcome=reuse_outcome, tokens=token_usage,
            attempts=attempts, failure_type=f"PATCH_CONTRACT: {validation_error}",
        )

    @staticmethod
    def _workspace_changed(before: dict[Path, bytes]) -> bool:
        """True when any selected file changed on disk during generation."""
        for path, original in before.items():
            if not path.is_file():
                continue
            if path.read_bytes() != original:
                return True
        return False

    @staticmethod
    def _normalize_staged_paths(patch: str, staging: Path,
                                files: list[Path]) -> str | None:
        """Resolve a uniquely abbreviated model path against staged files."""
        applier = PatchApplier(staging)
        replacements: dict[str, str] = {}
        for path in applier.paths(patch):
            if (staging / path).is_file():
                continue
            matches = [file.relative_to(staging).as_posix() for file in files
                       if file.relative_to(staging).as_posix().endswith("/" + path)
                       or file.name == Path(path).name]
            if len(matches) != 1:
                return None
            replacements[path] = matches[0]
        for old, new in replacements.items():
            patch = patch.replace(f"a/{old}", f"a/{new}")
            patch = patch.replace(f"b/{old}", f"b/{new}")
        return patch
