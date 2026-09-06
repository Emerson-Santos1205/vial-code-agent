from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass(frozen=True)
class ModelResponse:
    text: str
    returncode: int = 0
    stderr: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ModelInfo:
    name: str
    provider: str = ""


class ModelProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str, *, system: str = "") -> ModelResponse:
        raise NotImplementedError

    @abstractmethod
    def chat(self, messages: list[dict], *, system: str = "") -> ModelResponse:
        raise NotImplementedError

    def chat_stream(self, messages: list[dict], *, system: str = "") -> AsyncIterator[str]:
        raise NotImplementedError("chat_stream not implemented by this provider")

    def cancel(self) -> None:
        pass

    def list_models(self, provider: str | None = None) -> str:
        return ""

    def list_providers(self) -> str:
        return ""
