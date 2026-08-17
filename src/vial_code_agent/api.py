"""Stable application boundary.

Core integration and UI code should depend on this module rather than importing
implementation details from the CLI or TUI.
"""
from __future__ import annotations

from .agent import CodeAgent as VialAgent
from .patches import PatchApplier
from .vial_runtime import PersistenceError, VialRuntime


def create_runtime(core: object, state_root, **kwargs) -> VialRuntime:
    """Create the governed runtime without exposing CLI/TUI construction."""
    return VialRuntime(core, state_root, **kwargs)


def apply_patch(runtime: VialRuntime, root, patch: str, **kwargs):
    """Apply a patch through the Runtime governance boundary."""
    return runtime.apply_patch(PatchApplier(root), patch, **kwargs)

__all__ = ["VialAgent", "VialRuntime", "PersistenceError", "create_runtime", "apply_patch"]
