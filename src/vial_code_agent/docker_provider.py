"""OpenCode provider executed inside a Docker container."""
from __future__ import annotations

import json
import shlex
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
        prompt_path = Path(directory) / ".vial-opencode-prompt.txt"
        prompt_path.write_text(
            f"{prompt} Return only a unified diff.", encoding="utf-8")
        file_args = " ".join(
            shlex.quote(f"--file=/workspace/{path.relative_to(directory).as_posix()}")
            for path in files or [])
        shell_command = (
            "opencode run --agent build --format json "
            f"--model {shlex.quote(self.model)} "
            '"$(cat /workspace/.vial-opencode-prompt.txt)"'
            + (f" {file_args}" if file_args else ""))
        command = [
            self.docker, "run", "--rm",
            "--mount", f"type=bind,src={workspace},dst=/workspace",
            "--mount", f"type=bind,src={auth.resolve().as_posix()},dst=/root/.local/share/opencode/auth.json,readonly",
            "--entrypoint", "sh", self.image, "-lc", shell_command,
        ]
        try:
            process = subprocess.run(
                command, cwd=directory,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as error:
            if getattr(error, "winerror", None) == 206:
                raise RuntimeError(
                    "model prompt is too large for Windows command-line limits") from error
            raise RuntimeError("Docker executable not found") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"Docker provider timed out after {self.timeout_seconds}s") from error
        finally:
            prompt_path.unlink(missing_ok=True)
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
