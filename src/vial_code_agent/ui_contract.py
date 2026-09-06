"""UI contract for the VIAL chat interface.

This module defines the Protocol that any UI implementation must satisfy
to integrate with the ChatController. The contract keeps ``chat.py`` and
``tui_state.py`` free of framework dependencies (Textual, Rich, etc.),
making the command handling unit-testable.

Usage::

    from vial_code_agent.ui_contract import UIContract

    class MyUI(UIContract):
        def render(self, text: str) -> None:
            print(text)

        def render_stream(self, chunks):
            for chunk in chunks:
                print(chunk, end="", flush=True)
            print()

        def set_status(self, status: str) -> None:
            pass

        def request_approval(self, tool_id: str, arguments: dict) -> bool:
            return input(f"Approve {tool_id}? [y/N] ").lower() == "y"
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class UIContract(Protocol):
    """Interface that UI implementations must satisfy."""

    def render(self, text: str) -> None:
        """Display a complete text response to the user."""
        ...

    def render_stream(self, chunks) -> None:
        """Display a streaming response, yielding text chunks."""
        ...

    def set_status(self, status: str) -> None:
        """Update the UI status bar or equivalent."""
        ...

    def request_approval(self, tool_id: str, arguments: dict) -> bool:
        """Ask the user to approve a governed tool invocation.

        Returns True if approved, False otherwise.
        """
        ...
