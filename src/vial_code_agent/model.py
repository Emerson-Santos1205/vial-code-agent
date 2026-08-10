from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelResponse:
    text: str
    returncode: int
    stderr: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class OpenCodeProvider:
    """Adapter for the local opencode CLI; never invokes a model implicitly."""

    MODEL_ALIASES = {
        "fast": "openai/gpt-5.6-luna-fast",
        "reasoning": "openai/gpt-5.6-luna",
    }

    def __init__(
        self,
        model: str,
        executable: str = "opencode",
        auto_approve: bool = False,
        agent: str = "plan",
    ) -> None:
        self.model = self.MODEL_ALIASES.get(model, model)
        self.executable = executable
        self.auto_approve = auto_approve
        self.agent = agent

    def generate(
        self,
        prompt: str,
        timeout_seconds: int = 180,
        directory: Path | None = None,
        task: str | None = None,
        files: list[Path] | None = None,
    ) -> ModelResponse:
        instruction = f"{prompt} Return only a unified diff."
        executable = self.executable
        if not os.path.dirname(executable):
            executable = shutil.which(executable) or (
                shutil.which(f"{executable}.cmd") if os.name == "nt" else None
            ) or executable
        command = [executable, "run"]
        if self.auto_approve:
            command.append("--auto")
        command.extend(["--agent", self.agent, "--format", "json", "--model", self.model])
        command.append(instruction)
        for path in files or []:
            command.extend(["--file", str(path)])
        try:
            process = subprocess.run(
                command,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout_seconds, check=False, cwd=directory,
            )
        except FileNotFoundError as error:
            raise RuntimeError(f"model executable not found: {self.executable}") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"model request timed out after {timeout_seconds}s") from error
        text_parts: list[str] = []
        usage: dict[str, int | None] = {}
        for line in process.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "text":
                text_parts.append(event.get("part", {}).get("text", ""))
            elif event.get("type") == "step_finish":
                tokens = event.get("part", {}).get("tokens", {}) or {}
                usage = {
                    "input_tokens": tokens.get("input"),
                    "output_tokens": tokens.get("output"),
                    "total_tokens": tokens.get("total"),
                }
        if not text_parts:
            # Keep compatibility with opencode event variants that nest the final
            # answer differently while preserving the JSON protocol.
            for line in process.stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = _find_diff_text(event)
                if text is not None:
                    text_parts.append(text)
                    break
        return ModelResponse(
            text="".join(text_parts),
            returncode=process.returncode,
            stderr=process.stderr,
            **usage,
        )


def extract_diff(text: str) -> str | None:
    git_start = text.find("diff --git ")
    if git_start >= 0:
        text = text[git_start:]
    fenced = re.search(r"```(?:diff|patch)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    candidate = text.strip() if git_start >= 0 else (fenced.group(1).strip() if fenced else text.strip())
    if not candidate.startswith(("diff --git ", "--- ")):
        match = re.search(r"(?:^|\n)(diff --git |--- )", candidate)
        if match:
            candidate = candidate[match.start(1):].strip()
        else:
            header = candidate.find("--- a/")
            if header >= 0:
                candidate = candidate[header:].strip()
    if candidate.startswith(("diff --git ", "--- ")):
        return candidate if candidate.endswith("\n") else candidate + "\n"
    return None


def _find_diff_text(value: object) -> str | None:
    if isinstance(value, str) and "+++ " in value and ("--- " in value or "diff --git " in value):
        return value
    if isinstance(value, dict):
        for child in value.values():
            found = _find_diff_text(child)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_diff_text(child)
            if found is not None:
                return found
    return None
