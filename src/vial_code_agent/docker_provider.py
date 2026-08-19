"""OpenCode provider executed inside a Docker container."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .model import ModelResponse, OpenCodeProvider, _find_diff_text


class DockerOpenCodeProvider:
    """Run OpenCode with only a staged workspace mounted into the container."""

    def __init__(self, model: str, image: str = "vial-code-agent-opencode:1.18.18",
                 docker: str = "docker", timeout_seconds: int = 300) -> None:
        self.model = OpenCodeProvider.MODEL_ALIASES.get(model, model)
        self.image = image
        self.docker = docker
        self.timeout_seconds = timeout_seconds
        self.last_response: ModelResponse | None = None

    def generate(self, prompt: str, directory: Path | None = None,
                 files: list[Path] | None = None, **_: object) -> ModelResponse:
        if directory is None:
            raise RuntimeError("Docker provider requires a staging directory")
        auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
        if not auth.is_file():
            raise RuntimeError(f"OpenCode credentials not found: {auth}")
        workspace = Path(directory).resolve().as_posix()
        command = [
            self.docker, "run", "--rm",
            "--mount", f"type=bind,src={workspace},dst=/workspace",
            "--mount", f"type=bind,src={auth.resolve().as_posix()},dst=/root/.local/share/opencode/auth.json,readonly",
            self.image, "run", "--agent", "build", "--format", "json",
            "--model", self.model, f"{prompt} Return only a unified diff.",
        ]
        for path in files or []:
            command.append(f"--file=/workspace/{path.relative_to(directory).as_posix()}")
        try:
            process = subprocess.run(
                command, cwd=directory, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as error:
            raise RuntimeError("Docker executable not found") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"Docker provider timed out after {self.timeout_seconds}s") from error
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
                usage = {"input_tokens": tokens.get("input"),
                         "output_tokens": tokens.get("output"),
                         "total_tokens": tokens.get("total")}
        text = "".join(text_parts)
        if not text:
            for line in process.stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fallback = _find_diff_text(event)
                if fallback is not None:
                    text = fallback
                    break
        response = ModelResponse(
            text=text, returncode=process.returncode,
            stderr=process.stderr.strip(), **usage)
        self.last_response = response
        return response
