from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from vial_code_agent.providers import ModelProvider, ModelResponse

_MAX_CONTEXT_CHARS = 28_000


def _with_history(prompt: str, history: list[tuple[str, str]]) -> str:
    lines = [f"{role}: {content}" for role, content in history]
    context = "\n".join(lines)
    if len(context) > _MAX_CONTEXT_CHARS:
        context = context[-_MAX_CONTEXT_CHARS:]
        first_newline = context.find("\n")
        if first_newline >= 0:
            context = context[first_newline + 1:]
    return f"{context}\nuser: {prompt}"


def _uses_stdin_prompt(executable: str) -> bool:
    return os.name == "nt" and executable.lower().endswith((".cmd", ".bat"))


def _parse_events(stdout: str) -> tuple[str, dict[str, int | None]]:
    text_parts: list[str] = []
    usage: dict[str, int | None] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        part = event.get("part")
        if event.get("type") == "text" and isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                text_parts.append(text)
        elif event.get("type") == "step_finish" and isinstance(part, dict):
            tokens = part.get("tokens")
            if isinstance(tokens, dict):
                usage = {
                    "input_tokens": tokens.get("input"),
                    "output_tokens": tokens.get("output"),
                    "total_tokens": tokens.get("total"),
                }
    return "".join(text_parts), usage


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


def _extract_error(process: subprocess.CompletedProcess) -> str:
    if process.stderr and process.stderr.strip():
        return process.stderr.strip()
    for line in process.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "error":
            continue
        error = event.get("error") or {}
        if isinstance(error, dict):
            message = error.get("message")
            data = error.get("data") or {}
            if isinstance(data, dict) and not message:
                message = data.get("message")
            ref = data.get("ref") if isinstance(data, dict) else None
            if message:
                return f"{str(message)}{f' (ref {ref})' if ref else ''}"
        if event.get("error"):
            return str(event.get("error"))
    return ""


def _resolve_executable(executable: str) -> str:
    resolved = shutil.which(executable)
    if resolved:
        return resolved
    home = Path.home()
    for candidate in (
        home / "AppData" / "Roaming" / "npm" / f"{executable}.cmd",
        home / "AppData" / "Roaming" / "npm" / f"{executable}.ps1",
        home / ".npm-global" / "bin" / executable,
        home / ".local" / "bin" / executable,
    ):
        if candidate.is_file():
            return str(candidate)
    return executable


class OpenCodeProvider(ModelProvider):
    MODEL_ALIASES = {
        "fast": "opencode/big-pickle",
        "reasoning": "opencode/mimo-v2.5-free",
        "free": "opencode/big-pickle",
    }

    def __init__(
        self,
        model: str,
        executable: str = "opencode",
        auto_approve: bool = False,
        agent: str = "plan",
        timeout_seconds: int = 180,
    ) -> None:
        self.model = self.MODEL_ALIASES.get(model, model)
        self.executable = executable
        self.auto_approve = auto_approve
        self.agent = agent
        self.timeout_seconds = timeout_seconds
        self._active_proc: subprocess.Popen[str] | None = None
        self.last_response: ModelResponse | None = None

    def generate(
        self,
        prompt: str,
        timeout_seconds: int = 180,
        directory: Path | None = None,
        task: str | None = None,
        files: list[Path] | None = None,
    ) -> ModelResponse:
        timeout_seconds = timeout_seconds or self.timeout_seconds
        instruction = f"{prompt} Return only a unified diff."
        executable = self.executable
        if not os.path.dirname(executable):
            executable = _resolve_executable(executable)
        command = [executable, "run"]
        if self.auto_approve:
            command.append("--auto")
        command.extend(["--agent", self.agent, "--format", "json", "--model", self.model])
        uses_stdin = _uses_stdin_prompt(executable)
        if not uses_stdin:
            command.append(instruction)
        for path in files or []:
            command.append(f"--file={path}")
        try:
            process = subprocess.run(
                command,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout_seconds, check=False, cwd=directory,
                input=instruction if uses_stdin else None,
            )
        except FileNotFoundError as error:
            if getattr(error, "winerror", None) == 206:
                raise RuntimeError("model prompt is too large for Windows command-line limits; reduce --max-context-chars") from error
            raise RuntimeError(f"model executable not found: {self.executable}") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"model request timed out after {timeout_seconds}s") from error
        text, usage = _parse_events(process.stdout)
        if not text:
            for line in process.stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                found = _find_diff_text(event)
                if found is not None:
                    text = found
                    break
        return ModelResponse(
            text=text,
            returncode=process.returncode,
            stderr=_extract_error(process),
            **usage,
        )

    def chat(
        self,
        prompt: str,
        directory: Path | None = None,
        timeout_seconds: int = 180,
        history: list[tuple[str, str]] | None = None,
    ) -> ModelResponse:
        if history:
            prompt = _with_history(prompt, history)
        timeout_seconds = timeout_seconds or self.timeout_seconds
        executable = self.executable
        if not os.path.dirname(executable):
            executable = _resolve_executable(executable)
        uses_stdin = _uses_stdin_prompt(executable)
        command = [executable, "run", "--agent", self.agent, "--format", "json", "--model", self.model]
        if not uses_stdin:
            command.append(prompt)
        if self.auto_approve:
            command.insert(2, "--auto")
        process = subprocess.run(
            command, cwd=directory, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_seconds, check=False,
            input=prompt if uses_stdin else None,
        )
        text, _ = _parse_events(process.stdout)
        return ModelResponse(text, process.returncode, _extract_error(process))

    def chat_stream(
        self,
        prompt: str,
        directory: Path | None = None,
        timeout_seconds: int = 180,
        history: list[tuple[str, str]] | None = None,
    ):
        if history:
            prompt = _with_history(prompt, history)
        timeout_seconds = self.timeout_seconds
        executable = self.executable
        if not os.path.dirname(executable):
            executable = _resolve_executable(executable)
        uses_stdin = _uses_stdin_prompt(executable)
        command = [executable, "run", "--agent", self.agent, "--format", "json", "--model", self.model]
        if not uses_stdin:
            command.append(prompt)
        if self.auto_approve:
            command.insert(2, "--auto")
        process = subprocess.Popen(
            command, cwd=directory, stdin=subprocess.PIPE if uses_stdin else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )
        self._active_proc = process
        if uses_stdin and process.stdin is not None:
            process.stdin.write(prompt)
            process.stdin.close()
        text_parts: list[str] = []
        try:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "text":
                    chunk = event.get("part", {}).get("text", "")
                    if chunk:
                        text_parts.append(chunk)
                        yield chunk
        finally:
            self._active_proc = None
        stderr = ""
        if process.stderr is not None:
            stderr = process.stderr.read()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
        self.last_response = ModelResponse(
            text="".join(text_parts),
            returncode=process.returncode,
            stderr=stderr.strip(),
        )

    def cancel(self) -> None:
        proc = self._active_proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def list_models(self, provider: str | None = None) -> str:
        executable = _resolve_executable(self.executable)
        command = [executable, "models"]
        if provider:
            command.append(provider)
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if process.returncode:
            raise RuntimeError(process.stderr.strip() or "could not list models")
        return process.stdout

    def list_providers(self) -> str:
        executable = _resolve_executable(self.executable)
        process = subprocess.run([executable, "providers", "list"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
        if process.returncode:
            raise RuntimeError(process.stderr.strip() or "could not list providers")
        return process.stdout
