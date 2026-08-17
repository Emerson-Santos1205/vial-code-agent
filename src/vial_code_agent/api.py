"""Stable application boundary.

Core integration and UI code should depend on this module rather than importing
implementation details from the CLI or TUI.
"""
from __future__ import annotations

from .agent import CodeAgent as VialAgent
from .vial_runtime import VialRuntime

__all__ = ["VialAgent", "VialRuntime"]
