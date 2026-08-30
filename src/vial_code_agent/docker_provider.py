"""OpenCode provider executed inside a Docker container."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from .model import (ModelResponse, OpenCodeProvider, _extract_error,
                    _find_diff_text, _parse_events)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate Docker and any descendants left behind by a timed-out run."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, check=False,
        )
    else:
        try:
            os.killpg(process.pid, 15)
        except (ProcessLookupError, OSError):
            process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


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
        process = None
        try:
            popen_kwargs = {
                "cwd": directory,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
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
            raise RuntimeError(f"Docker provider timed out after {self.timeout_seconds}s") from error
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
            stderr=_extract_error(completed), **usage)
        self.last_response = response
        return response
