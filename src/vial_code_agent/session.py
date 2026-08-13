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


def _monotonic_timestamp(previous: float) -> float:
    """Return a timestamp strictly greater than ``previous``."""
    timestamp = time.time()
    if timestamp > previous:
        return timestamp
    return previous + 1e-3


class SessionStore:
    """Durable JSONL chat memory, one session per file."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._last_timestamp = 0.0

    def create(self) -> str:
        session_id = uuid.uuid4().hex
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / f"{session_id}.jsonl").touch()
        return session_id

    def append(self, session_id: str, role: str, content: str) -> None:
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"invalid message role: {role}")
        self.directory.mkdir(parents=True, exist_ok=True)
        timestamp = _monotonic_timestamp(self._last_timestamp)
        self._last_timestamp = timestamp
        with (self.directory / f"{session_id}.jsonl").open("a", encoding="utf-8") as stream:
            json.dump({"role": role, "content": content, "timestamp": timestamp}, stream)
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

    def list(self) -> list[str]:
        """Session ids ordered by most recent modification first.

        Ties (same modification timestamp) break deterministically by the
        newest stored message timestamp, then by session id.
        """
        if not self.directory.is_dir():
            return []
        sessions = [
            path.stem
            for path in self.directory.glob("*.jsonl")
            if path.is_file()
        ]

        def last_message_time(session_id: str) -> float:
            messages = self.messages(session_id)
            return messages[-1].timestamp if messages else 0.0

        return sorted(
            sessions,
            key=lambda session_id: (
                (self.directory / f"{session_id}.jsonl").stat().st_mtime,
                last_message_time(session_id),
                session_id,
            ),
            reverse=True,
        )
