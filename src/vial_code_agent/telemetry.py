from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class Telemetry:
    """Best-effort local JSONL events; prompts and file contents are never stored."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def record(self, event: str, **values: Any) -> None:
        if self.path is None:
            return
        payload = {"timestamp": time.time(), "event": event, **values}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True)
                stream.write("\n")
        except OSError:
            pass
