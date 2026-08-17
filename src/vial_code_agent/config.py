from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .vial_runtime import ACTOR, AUTHORITY, ORG_ID


@dataclass(frozen=True)
class AgentConfig:
    model: str = "auto"
    cache_dir: str = ".vial-cache"
    test_timeout: int = 120
    model_timeout: int = 300
    max_context_chars: int = 6_000
    opencode_executable: str = "opencode"
    opencode_agent: str = "build"
    auto_approve: bool = False
    auto_approve_max_risk: str = "medium"
    telemetry_file: str = ".vial-cache/events.jsonl"
    org_id: str = ORG_ID
    authority: str = AUTHORITY
    actor: str = ACTOR
    persist_state: bool = True
    price_table_json: str = ""


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value) if value is not None else default


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
        model_timeout=int(os.environ.get("VIAL_MODEL_TIMEOUT", values.get("model_timeout", 300))),
        max_context_chars=int(os.environ.get("VIAL_MAX_CONTEXT_CHARS", values.get("max_context_chars", 6_000))),
        opencode_executable=os.environ.get(
            "VIAL_OPENCODE_EXECUTABLE", str(values.get("opencode_executable", "opencode"))
        ),
        opencode_agent=os.environ.get("VIAL_OPENCODE_AGENT", str(values.get("opencode_agent", "build"))),
        auto_approve=_as_bool(
            os.environ.get("VIAL_AUTO_APPROVE"), values.get("auto_approve", False)),
        auto_approve_max_risk=os.environ.get(
            "VIAL_AUTO_APPROVE_MAX_RISK",
            str(values.get("auto_approve_max_risk", "medium"))),
        telemetry_file=os.environ.get(
            "VIAL_TELEMETRY_FILE", str(values.get("telemetry_file", ".vial-cache/events.jsonl"))
        ),
        org_id=os.environ.get("VIAL_ORG_ID", str(values.get("org_id", ORG_ID))),
        authority=os.environ.get("VIAL_AUTHORITY", str(values.get("authority", AUTHORITY))),
        actor=os.environ.get("VIAL_ACTOR", str(values.get("actor", ACTOR))),
        persist_state=_as_bool(
            os.environ.get("VIAL_PERSIST_STATE"), values.get("persist_state", True)),
        price_table_json=os.environ.get(
            "VIAL_PRICE_TABLE", str(values.get("price_table_json", ""))),
    )
