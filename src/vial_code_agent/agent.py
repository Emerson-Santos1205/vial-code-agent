from __future__ import annotations

from dataclasses import dataclass
import difflib
from pathlib import Path

from .model import ModelResponse, OpenCodeProvider, extract_diff
from .core import VialCoreReference


@dataclass(frozen=True)
class GenerationResult:
    response: ModelResponse
    patch: str | None
    workspace_changed: bool = False
    context_id: str = ""


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
    def __init__(self, provider: OpenCodeProvider) -> None:
        self.provider = provider

    def generate(
        self, task: str, root: Path, files: list[Path], max_chars: int = 6_000,
        vial: VialCoreReference | None = None,
    ) -> GenerationResult:
        before = {path: path.read_bytes() for path in files if path.is_file()}
        context_id = ""
        if vial is not None and vial.exists():
            context = vial.build_context(task, root, files)
            context_id = context.context_id
            context.consume()
            prompt = context.body
        else:
            prompt = build_prompt(task, root, files, max_chars)
        response = self.provider.generate(prompt, directory=root)
        patch = extract_diff(response.text)
        if patch is not None:
            return GenerationResult(response=response, patch=patch, context_id=context_id)
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
            return GenerationResult(response=response, patch="".join(generated), workspace_changed=True, context_id=context_id)
        return GenerationResult(response=response, patch=None)
