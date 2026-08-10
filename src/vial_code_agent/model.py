from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelResponse:
    text: str
    returncode: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class OpenCodeProvider:
    """Adapter for the local opencode CLI; never invokes a model implicitly."""

    def __init__(self, model: str, executable: str = "opencode") -> None:
        self.model = model
        self.executable = executable

    def generate(self, prompt: str, timeout_seconds: int = 180) -> ModelResponse:
        instruction = (
            "Return only a unified diff. Do not include explanations or markdown.\n\n"
            + prompt
        )
        process = subprocess.run(
            [self.executable, "run", "--format", "json", "--model", self.model, instruction],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
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
        return ModelResponse(text="".join(text_parts), returncode=process.returncode, **usage)


def extract_diff(text: str) -> str | None:
    fenced = re.search(r"```(?:diff|patch)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    candidate = fenced.group(1).strip() if fenced else text.strip()
    return candidate if candidate.startswith(("diff --git ", "--- ")) else None
