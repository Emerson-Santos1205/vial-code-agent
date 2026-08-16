from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Prior-turn context is injected into the prompt; capped to stay under the
# Windows ~32k command-line limit for ``opencode run <prompt>``.
_MAX_CONTEXT_CHARS = 28_000


def _with_history(prompt: str, history: list[tuple[str, str]]) -> str:
    """Prepend prior ``(role, content)`` turns to ``prompt``."""
    lines = [f"{role}: {content}" for role, content in history]
    context = "\n".join(lines)
    if len(context) > _MAX_CONTEXT_CHARS:
        context = context[-_MAX_CONTEXT_CHARS:]
        first_newline = context.find("\n")
        if first_newline >= 0:
            context = context[first_newline + 1:]
    return f"{context}\nuser: {prompt}"


def _uses_stdin_prompt(executable: str) -> bool:
    """Avoid cmd.exe's short command-line limit for Windows wrappers."""
    return os.name == "nt" and executable.lower().endswith((".cmd", ".bat"))


def _trim_messages(messages: list[dict[str, str]]) -> None:
    """Drop oldest messages until the payload fits the context cap."""
    total = sum(len(message["content"]) for message in messages)
    while total > _MAX_CONTEXT_CHARS and len(messages) > 1:
        total -= len(messages.pop(0)["content"])


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
        timeout_seconds = self.timeout_seconds
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
        """Return a conversational response without forcing diff extraction.

        ``history`` is an optional list of prior ``(role, content)`` turns,
        prepended to the prompt so follow-up prompts keep the session context.
        """
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
        process = subprocess.run(
            command, cwd=directory, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_seconds, check=False,
            input=prompt if uses_stdin else None,
        )
        text = "".join(
            json.loads(line).get("part", {}).get("text", "")
            for line in process.stdout.splitlines()
            if _is_text_event(line)
        )
        return ModelResponse(text, process.returncode, _extract_error(process))

    def chat_stream(
        self,
        prompt: str,
        directory: Path | None = None,
        timeout_seconds: int = 180,
        history: list[tuple[str, str]] | None = None,
    ):
        """Yield text chunks as the model streams them (JSON events on stdout).

        Sets ``self.last_response`` once the process finishes so the caller can
        inspect the final return code / error. Cancellation is cooperative: call
        :meth:`cancel` to terminate the active subprocess, which ends iteration.
        """
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
        """Terminate the subprocess currently streaming, if any."""
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


class HttpModelProvider:
    """OpenAI-compatible HTTP provider for custom servers (stdlib only).

    Speaks the ``/v1/chat/completions`` contract directly over HTTPS, so a
    user-configured server does not depend on the ``opencode`` CLI or its
    provider registry.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: int = 180,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def _endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    def _post(self, payload: dict[str, object]) -> dict[str, object]:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self._endpoint(), data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"server {self.base_url} returned HTTP {error.code}: {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(
                f"cannot connect to {self.base_url}: {error.reason}"
            ) from error
        if not isinstance(data, dict):
            raise RuntimeError(f"invalid response from {self.base_url}")
        return data

    def chat(
        self,
        prompt: str,
        directory: Path | None = None,
        timeout_seconds: int = 180,
        history: list[tuple[str, str]] | None = None,
    ) -> ModelResponse:
        messages = [
            {"role": role, "content": content}
            for role, content in history or []
            if role in ("system", "user", "assistant")
        ]
        messages.append({"role": "user", "content": prompt})
        _trim_messages(messages)
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        try:
            data = self._post(payload)
        except RuntimeError as error:
            return ModelResponse("", 1, stderr=str(error))
        content = ""
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            message = first.get("message", {}) if isinstance(first, dict) else {}
            content = message.get("content", "") if isinstance(message, dict) else ""
        usage = data.get("usage") or {}
        return ModelResponse(
            text=content if isinstance(content, str) else str(content),
            returncode=0,
            stderr="",
            input_tokens=_as_int(usage.get("prompt_tokens")),
            output_tokens=_as_int(usage.get("completion_tokens")),
            total_tokens=_as_int(usage.get("total_tokens")),
        )

    def list_models(self, provider: str | None = None) -> str:
        """List models from ``GET /v1/models`` when the server exposes it."""
        url = self._endpoint().replace("/chat/completions", "/models")
        request = urllib.request.Request(url, method="GET")
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"server {url} returned HTTP {error.code}" f" ({error.reason})"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"cannot connect to {url}: {error.reason}") from error
        ids = [item.get("id") for item in data.get("data", [])]
        return "\n".join(str(item) for item in ids if item)


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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


def _is_text_event(line: str) -> bool:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False
    return event.get("type") == "text"


def _extract_error(process: subprocess.CompletedProcess) -> str:
    """Surface an opencode ``error`` event so failures aren't silent.

    ``opencode run --format json`` reports failures as a JSON ``error`` event
    on stdout (not stderr) with a non-zero exit code; without this the UI can
    only show ``model exited with code N``.
    """
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
