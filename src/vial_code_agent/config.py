from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentConfig:
    model: str = "auto"
    cache_dir: str = ".vial-cache"
    test_timeout: int = 120
    max_context_chars: int = 120_000
    opencode_executable: str = "opencode"
    opencode_agent: str = "plan"
    telemetry_file: str = ".vial-cache/events.jsonl"


def load_config(root: Path) -> AgentConfig:
    """Load optional .vial.json values, with environment overrides."""
    values: dict[str, object] = {}
    path = root / ".vial.json"
    if path.is_file():
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid configuration: {path}: {error}") from error
    return AgentConfig(
        model=os.environ.get("VIAL_MODEL", str(values.get("model", "auto"))),
        cache_dir=os.environ.get("VIAL_CACHE_DIR", str(values.get("cache_dir", ".vial-cache"))),
        test_timeout=int(os.environ.get("VIAL_TEST_TIMEOUT", values.get("test_timeout", 120))),
        max_context_chars=int(os.environ.get("VIAL_MAX_CONTEXT_CHARS", values.get("max_context_chars", 120_000))),
        opencode_executable=os.environ.get(
            "VIAL_OPENCODE_EXECUTABLE", str(values.get("opencode_executable", "opencode"))
        ),
        opencode_agent=os.environ.get("VIAL_OPENCODE_AGENT", str(values.get("opencode_agent", "plan"))),
        telemetry_file=os.environ.get(
            "VIAL_TELEMETRY_FILE", str(values.get("telemetry_file", ".vial-cache/events.jsonl"))
        ),
    )
