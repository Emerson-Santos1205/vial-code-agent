from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    timestamp: float


class SessionStore:
    """Durable JSONL chat memory, one session per file."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def create(self) -> str:
        session_id = uuid.uuid4().hex
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / f"{session_id}.jsonl").touch()
        return session_id

    def append(self, session_id: str, role: str, content: str) -> None:
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"invalid message role: {role}")
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / f"{session_id}.jsonl").open("a", encoding="utf-8") as stream:
            json.dump({"role": role, "content": content, "timestamp": time.time()}, stream)
            stream.write("\n")

    def messages(self, session_id: str) -> list[Message]:
        path = self.directory / f"{session_id}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(path)
        result: list[Message] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            value: dict[str, Any] = json.loads(line)
            result.append(Message(value["role"], value["content"], value["timestamp"]))
        return result
