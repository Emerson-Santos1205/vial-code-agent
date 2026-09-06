from __future__ import annotations

import json
import os  # noqa: F401 — re-export for test mock compatibility
import re
import shutil  # noqa: F401 — re-export for test mock compatibility
import subprocess  # noqa: F401 — re-export for test mock compatibility
import urllib.request  # noqa: F401 — re-export for test mock compatibility
from pathlib import Path  # noqa: F401 — re-export for test mock compatibility

from vial_code_agent.providers import ModelInfo, ModelProvider, ModelResponse
from vial_code_agent.providers.http_provider import HttpModelProvider, _as_int, _trim_messages
from vial_code_agent.providers.opencode_provider import (
    _MAX_CONTEXT_CHARS,
    OpenCodeProvider,
    _extract_error,
    _find_diff_text,
    _parse_events,
    _resolve_executable,
    _with_history,
)


def _is_text_event(line: str) -> bool:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False
    return isinstance(event, dict) and event.get("type") == "text"


def extract_diff(text: str) -> str | None:
    apply_patch = re.search(
        r"\*\*\* Update File: (.+?)\n(.*?)(?:\n\*\*\* End Patch|$)",
        text, re.IGNORECASE | re.DOTALL)
    if apply_patch:
        path = apply_patch.group(1).strip()
        body = apply_patch.group(2).strip("\n")
        return f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}\n"
    fenced = re.search(r"```(?:diff|patch)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
    else:
        git_start = text.find("diff --git ")
        candidate = text[git_start:].strip() if git_start >= 0 else text.strip()
    candidate = candidate.split("\n*** End Patch", 1)[0].rstrip()
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


__all__ = [
    "ModelProvider",
    "ModelResponse",
    "ModelInfo",
    "OpenCodeProvider",
    "HttpModelProvider",
    "extract_diff",
    "_MAX_CONTEXT_CHARS",
    "_is_text_event",
    "_trim_messages",
    "_with_history",
    "_as_int",
    "_extract_error",
    "_find_diff_text",
    "_parse_events",
    "_resolve_executable",
]
