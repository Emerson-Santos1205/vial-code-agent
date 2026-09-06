from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from vial_code_agent.providers import ModelInfo, ModelProvider, ModelResponse

_MAX_CONTEXT_CHARS = 28_000


def _trim_messages(messages: list[dict[str, str]]) -> None:
    total = sum(len(message["content"]) for message in messages)
    while total > _MAX_CONTEXT_CHARS and len(messages) > 1:
        total -= len(messages.pop(0)["content"])


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class HttpModelProvider(ModelProvider):
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

    def generate(self, prompt: str, *, system: str = "") -> ModelResponse:
        messages = [{"role": "user", "content": prompt}]
        if system:
            messages.insert(0, {"role": "system", "content": system})
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
        usage: dict = data.get("usage") or {}
        return ModelResponse(
            text=content if isinstance(content, str) else str(content),
            returncode=0,
            stderr="",
            input_tokens=_as_int(usage.get("prompt_tokens")),
            output_tokens=_as_int(usage.get("completion_tokens")),
            total_tokens=_as_int(usage.get("total_tokens")),
        )

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
        usage: dict = data.get("usage") or {}
        return ModelResponse(
            text=content if isinstance(content, str) else str(content),
            returncode=0,
            stderr="",
            input_tokens=_as_int(usage.get("prompt_tokens")),
            output_tokens=_as_int(usage.get("completion_tokens")),
            total_tokens=_as_int(usage.get("total_tokens")),
        )

    def list_models(self, provider: str | None = None) -> str:
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
