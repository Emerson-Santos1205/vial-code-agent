"""OpenCode provider executed inside a Docker container."""
from __future__ import annotations

import json
import shlex
import signal
import subprocess
from pathlib import Path
from typing import Any

from vial_code_agent.processes import process_group_kwargs, terminate_process_tree
from vial_code_agent.providers import ModelProvider, ModelResponse
from vial_code_agent.providers.opencode_provider import (
    OpenCodeProvider,
    _extract_error,
    _find_diff_text,
    _parse_events,
)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    terminate_process_tree(process, termination_signal=signal.SIGTERM)


class DockerOpenCodeProvider(ModelProvider):
    def __init__(self, model: str, image: str = "vial-code-agent-opencode:1.18.18",
                 docker: str = "docker", timeout_seconds: int = 300) -> None:
        self.model = OpenCodeProvider.MODEL_ALIASES.get(model, model)
        self.image = image
        self.docker = docker
        self.timeout_seconds = timeout_seconds
        self.last_response: ModelResponse | None = None

    def generate(self, prompt: str, *, system: str = "") -> ModelResponse:
        raise NotImplementedError(
            "DockerOpenCodeProvider.generate requires directory")

    def generate_in_dir(self, prompt: str, directory: Path | None = None,
                        files: list[Path] | None = None) -> ModelResponse:
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
            shlex.quote(
                f"--file=/workspace/{path.relative_to(directory).as_posix()}")
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
        process = None
        try:
            popen_kwargs: dict[str, Any] = {
                "cwd": directory,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            }
            popen_kwargs.update(process_group_kwargs())
            # type: ignore[call-overload]
            process = subprocess.Popen(command, **popen_kwargs)
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except FileNotFoundError as error:
            if getattr(error, "winerror", None) == 206:
                raise RuntimeError(
                    "model prompt is too large for Windows command-line limits") from error
            raise RuntimeError("Docker executable not found") from error
        except subprocess.TimeoutExpired as error:
            if process is not None:
                _terminate_process_tree(process)
            raise RuntimeError(
                f"Docker provider timed out after {self.timeout_seconds}s") from error
        finally:
            prompt_path.unlink(missing_ok=True)
        completed = subprocess.CompletedProcess(
            command, process.returncode, stdout, stderr)
        text, usage = _parse_events(completed.stdout)
        if not text:
            for line in completed.stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fallback = _find_diff_text(event)
                if fallback is not None:
                    text = fallback
                    break
        response = ModelResponse(
            text=text, returncode=completed.returncode,
            stderr=_extract_error(completed),
            input_tokens=usage.get("input_tokens") or 0,
            output_tokens=usage.get("output_tokens") or 0,
            total_tokens=usage.get("total_tokens") or 0,
        )
        self.last_response = response
        return response

    def chat(self, messages: list[dict], *, system: str = "") -> ModelResponse:
        raise NotImplementedError("Docker provider does not support chat")
