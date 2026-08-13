"""Application-level error boundary for the VIAL code agent.

The runtime surfaces the structured ``VIALError`` model from the official
prototype (``vendor/vial-core/prototype/errors.py``, SDK-001 §30-31). This
module only wraps local orchestration failures that do not originate inside
the VIAL core so that callers always receive a ``code``/``message`` pair.
"""
from __future__ import annotations

from typing import Any

# Canonical app-level error codes (SDK-001 §30 categories).
ERR_INVALID_CONFIG = "INVALID_CONFIG"
ERR_INVALID_USAGE = "INVALID_USAGE"
ERR_RUNTIME = "RUNTIME_ERROR"
ERR_TOOL = "TOOL_ERROR"
ERR_MODEL = "MODEL_ERROR"
ERR_NOT_ALLOWED = "NOT_ALLOWED"


class VialRuntimeError(RuntimeError):
    """Raised when the application runtime cannot complete an operation.

    Carries the same ``code``/``message``/``details`` shape used by the
    official ``VIALError`` model so logs stay machine-readable.
    """

    def __init__(self, code: str, message: str,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def wrap(error: BaseException, code: str = ERR_RUNTIME,
         details: dict[str, Any] | None = None) -> VialRuntimeError:
    """Wrap a raw exception into the structured app error model.

    Returns the original error unchanged when it is already structured.
    """
    if isinstance(error, VialRuntimeError):
        return error
    return VialRuntimeError(code, str(error), details=details)
