"""Textual terminal UI inspired by the opencode TUI.

Layout mirrors opencode: a message log with a command input at the bottom, a
side panel showing session/model/agent/status/pool, and a footer of
keybindings. Tab switches the agent (build/plan), ``/models`` opens a picker,
and every opencode-style slash command is handled by ``ChatController``.
"""
from __future__ import annotations

import os
import sys

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.selection import Selection
from textual.widgets import Footer, Header, ListItem, ListView, RichLog, Static, TextArea

from . import __version__
from .chat import ChatController


class ModelPicker(ModalScreen[str]):
    """Modal list of models; returns the selected model ref."""

    BINDINGS = [Binding("escape", "dismiss_model", "cancel")]

    def __init__(self, models: list[str], current: str) -> None:
        super().__init__()
        self._models = models
        self._current = current

    def compose(self) -> ComposeResult:
        yield Static("Select a model (auto = route by prompt)", classes="picker-title")
        yield ListView(
            *[ListItem(Static(m)) for m in self._models], id="model-list"
        )

    def on_mount(self) -> None:
        list_view = self.query_one("#model-list", ListView)
        try:
            list_view.index = max(0, self._models.index(self._current))
        except ValueError:
            list_view.index = 0
        list_view.focus()

    def action_dismiss_model(self) -> None:
        self.dismiss(None)

    @on(ListView.Selected)
    def _selected(self, event: ListView.Selected) -> None:
        list_view = self.query_one("#model-list", ListView)
        index = list_view.index
        if index is not None and 0 <= index < len(self._models):
            self.dismiss(self._models[index])
        else:
            self.dismiss(None)


class PoolPicker(ModalScreen[list[str] | None]):
    """Multi-select of the models ``auto`` routing may use (the pool)."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("space", "toggle", "toggle"),
    ]

    def __init__(self, models: list[str], current: list[str]) -> None:
        super().__init__()
        self._models = models
        self._selected = set(current)

    def compose(self) -> ComposeResult:
        yield Static(
            "Select models for auto routing (space = toggle · enter = save · esc = cancel)",
            classes="picker-title",
        )
        yield ListView(
            *[
                ListItem(Static(self._row_text(model)))
                for model in self._models
            ],
            id="pool-list",
        )

    def _row_text(self, model: str) -> str:
        mark = "\u2611" if model in self._selected else "\u2610"
        return f"{mark} {model}"

    def on_mount(self) -> None:
        self.query_one("#pool-list", ListView).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_toggle(self) -> None:
        list_view = self.query_one("#pool-list", ListView)
        index = list_view.index
        if index is None or not (0 <= index < len(self._models)):
            return
        model = self._models[index]
        if model in self._selected:
            self._selected.discard(model)
        else:
            self._selected.add(model)
        item = list_view.children[index]
        item.query_one(Static).update(self._row_text(model))

    @on(ListView.Selected)
    def _saved(self, event: ListView.Selected) -> None:
        self.dismiss([m for m in self._models if m in self._selected])


class PromptArea(TextArea):
    """Single-logical-line prompt that soft-wraps long input."""

    BINDINGS = [
        Binding("enter", "submit_prompt", "send", priority=True),
        Binding("up", "menu_up", "menu up"),
        Binding("down", "menu_down", "menu down"),
    ]

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def action_submit_prompt(self) -> None:
        self.app.prompt_enter()

    def action_menu_up(self) -> None:
        self.app.menu_move(-1)

    def action_menu_down(self) -> None:
        self.app.menu_move(1)


class SelectableLog(RichLog):
    """RichLog whose drag selection is scoped to the widget itself."""

    ALLOW_SELECT = True

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        text = "\n".join(strip.text.rstrip() for strip in self.lines)
        if not text:
            return None
        return selection.extract(text), "\n"


class VialTUI(App[str]):
    """Fullscreen opencode-style interface for the VIAL runtime."""

    TITLE = "vial"
    SUB_TITLE = f"opencode-style terminal · {__version__}"
    CSS = """
    #layout { height: 1fr; }
    #main { width: 3fr; }
    #log { height: 1fr; border: round $primary; }
    #bottom { dock: bottom; height: auto; }
    #prompt { margin: 0 1 1 1; height: auto; max-height: 8; }
    #command-menu { height: auto; max-height: 12; border: round $primary; display: none; }
    #command-menu ListItem { padding: 0 2; }
    #command-menu ListItem.-highlight { color: $text; background: $primary 30%; }
    #side { width: 1fr; border: round $panel-lighten-2; padding: 1 1; color: $text-muted; }
    #side .key { color: $primary; }
    ModelPicker, PoolPicker { background: $surface; }
    .picker-title { padding: 1 2; text-style: bold; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "quit", priority=True),
        Binding("tab", "switch_agent", "agent", priority=True),
        Binding("ctrl+p", "pick_model", "model", priority=True),
        Binding("ctrl+b", "pick_pool", "pool", priority=True),
        Binding("ctrl+y", "copy_last", "copy", priority=True),
        Binding("ctrl+u", "focus_prompt", "prompt", priority=True),
    ]

    def __init__(self, controller: ChatController, prompt: str = "") -> None:
        super().__init__()
        self.controller = controller
        self._initial_prompt = prompt
        self._busy = False
        self._menu_matches: list[str] = []

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="layout"):
            with Vertical(id="main"):
                yield SelectableLog(id="log", markup=True, highlight=True, wrap=True)
            yield Static("", id="side")
        with Vertical(id="bottom"):
            yield ListView(id="command-menu")
            yield PromptArea(
                placeholder='Ask anything... "Fix a TODO in the codebase"',
                id="prompt",
                soft_wrap=True,
            )
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_side()
        self.query_one("#prompt", PromptArea).focus()
        if self._initial_prompt:
            self._submit_prompt(self._initial_prompt)

    # ------------------------------------------------------------------ #
    # Input handling
    # ------------------------------------------------------------------ #
    @on(PromptArea.Submitted)
    def _submitted(self, event: PromptArea.Submitted) -> None:
        self._submit_prompt(event.value)

    @on(TextArea.Changed)
    def _changed(self, event: TextArea.Changed) -> None:
        self._update_menu(event.text_area.text)

    def _update_menu(self, text: str) -> None:
        """Show the command card above the prompt when the buffer starts with ``/``."""
        matches = self.controller.command_matches(text)
        self._menu_matches = matches
        menu = self.query_one("#command-menu", ListView)
        if not matches or not text.strip().startswith("/"):
            menu.display = False
            return
        menu.clear()
        for name in matches:
            menu.append(ListItem(Static(name)))
        menu.index = 0
        menu.display = True

    def command_menu_visible(self) -> bool:
        return self.query_one("#command-menu", ListView).display

    def menu_move(self, delta: int) -> None:
        menu = self.query_one("#command-menu", ListView)
        if not menu.display or not self._menu_matches:
            return
        count = len(self._menu_matches)
        menu.index = ((menu.index if menu.index is not None else 0) + delta) % count

    def prompt_enter(self) -> None:
        """Enter: run the highlighted command, or submit the raw prompt."""
        menu = self.query_one("#command-menu", ListView)
        prompt = self.query_one("#prompt", PromptArea)
        if menu.display and self._menu_matches:
            index = menu.index
            if index is not None and 0 <= index < len(self._menu_matches):
                self._submit_prompt(self._menu_matches[index])
                return
        prompt.post_message(prompt.Submitted(prompt.text))

    def _submit_prompt(self, raw: str) -> None:
        message = raw.strip()
        prompt_widget = self.query_one("#prompt", PromptArea)
        if not message:
            prompt_widget.text = ""
            return
        prompt_widget.text = ""
        result = self.controller.handle(message)
        if result.new_model:
            self.controller.model = result.new_model
            self.controller.routing.default_model = result.new_model
        if result.new_agent:
            self.controller.agent = result.new_agent
            self.controller.routing.agent = result.new_agent
        if result.new_session_id:
            self.controller.session_id = result.new_session_id
        if result.handled:
            if result.clipboard:
                copied = self._copy_to_clipboard(result.clipboard)
                if copied is not None:
                    self._log_user(message)
                    self._log_assistant(copied)
                else:
                    self._log_assistant(
                        "no clipboard tool found; run /copy in a real terminal")
            if result.output:
                self._log_user(message)
                self._log_assistant(result.output)
            if result.new_session_id or result.new_model or result.new_agent:
                self.refresh_side()
            if result.exit:
                self.exit("")
            return
        self._log_user(message)
        self._busy = True
        self.refresh_side()
        self.run_worker(self._dispatch(message), group="model", exclusive=True)

    async def _dispatch(self, message: str) -> None:
        try:
            text, _route = await self._run_dispatch(message)
            self._log_assistant(text)
        except Exception as error:  # noqa: BLE001 - surface model errors in the TUI
            self._log_assistant(f"error: {error}")
        finally:
            self._busy = False
            self.refresh_side()

    async def _run_dispatch(self, message: str) -> tuple[str, str]:
        # Model calls are blocking (subprocess / HTTP); run them in the worker
        # pool so the UI stays responsive.
        import asyncio
        return await asyncio.to_thread(self.controller.respond, message)

    # ------------------------------------------------------------------ #
    # Actions / keybindings
    # ------------------------------------------------------------------ #
    def action_switch_agent(self) -> None:
        self.controller.agent = "plan" if self.controller.agent == "build" else "build"
        self.controller.routing.agent = self.controller.agent
        self._log_assistant(f"agent: {self.controller.agent}")
        self.refresh_side()

    def action_pick_model(self) -> None:
        models = self.controller.available_models()
        if not models:
            self._log_assistant("no models available")
            return

        def done(selected: str | None) -> None:
            if not selected:
                return
            self.controller.model = selected
            self.controller.routing.default_model = selected
            label = "auto (orchestrator -> pool)" if selected == "auto" else f"{selected} (pinned, pool inactive)"
            self._log_assistant(f"model: {label}")
            self.refresh_side()

        self.push_screen(ModelPicker(models, self.controller.model), done)

    def action_pick_pool(self) -> None:
        models = [m for m in self.controller.available_models() if m != "auto"]
        if not models:
            self._log_assistant("no models available")
            return
        current = list(self.controller.registry.pool)

        def done(selected: list[str] | None) -> None:
            if selected is None:
                return
            self.controller.registry.pool_set(selected)
            label = ", ".join(selected) or "empty"
            self._log_assistant(f"pool (auto candidates): {label}")
            self.refresh_side()

        self.push_screen(PoolPicker(models, current), done)

    def action_focus_prompt(self) -> None:
        self.query_one("#prompt", Input).focus()

    def action_copy_last(self) -> None:
        selection = self.screen.get_selected_text()
        if selection:
            copied = self._copy_to_clipboard(selection)
            self.screen.clear_selection()
            self._log_assistant(
                copied if copied is not None
                else "no clipboard tool found; selection needs a real terminal")
            return
        text = self.controller.last_assistant()
        if not text:
            self._log_assistant("no assistant response to copy")
            return
        copied = self._copy_to_clipboard(text)
        self._log_assistant(
            copied if copied is not None
            else "no clipboard tool found; selection needs a real terminal")
        return

    def _copy_to_clipboard(self, text: str) -> str | None:
        """Put ``text`` on the system clipboard.

        Uses the platform tool (clip.exe / pbcopy / xclip / wl-copy) so copy
        works inside the Textual UI, where terminal-native selection is
        swallowed by the mouse capture. Returns a confirmation string, or None
        when no clipboard tool is available.
        """
        import shutil
        import subprocess

        if os.name == "nt":
            tool = "clip.exe"
        elif sys.platform == "darwin":
            tool = "pbcopy"
        else:
            tool = "xclip" if shutil.which("xclip") else "wl-copy"
        try:
            subprocess.run(
                [tool], input=text, check=True,
                text=True, encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return f"copied to clipboard ({len(text)} chars)"

    def action_quit(self) -> None:
        self.exit("")

    # ------------------------------------------------------------------ #
    # Rendering helpers
    # ------------------------------------------------------------------ #
    def _log_user(self, message: str) -> None:
        self.query_one("#log", RichLog).write(f"[bold][cyan]You[/cyan][/bold] {message}")

    def _log_assistant(self, message: str) -> None:
        self.query_one("#log", RichLog).write(f"[bold][magenta]VIAL[/magenta][/bold] {message}")

    def refresh_side(self) -> None:
        controller = self.controller
        pool_models = controller.registry.pool
        pool = ", ".join(pool_models) or "empty"
        status = "Thinking..." if self._busy else "Ready"
        pinned = bool(controller.model and controller.model != "auto")
        if pinned:
            routing = f"pinned · {controller.model}"
            pool_label = "Pool (inactive)"
        else:
            routing = f"auto · {len(pool_models)} candidate(s)"
            pool_label = "Pool"
        panel = (
            f"[b]Session[/b]\n  {controller.session_id[:12]}\n\n"
            f"[b]Agent[/b]\n  {controller.agent}\n\n"
            f"[b]Model[/b]\n  {controller.model}\n\n"
            f"[b]Routing[/b]\n  {routing}\n\n"
            f"[b]Status[/b]\n  {status}\n\n"
            f"[b]{pool_label}[/b]\n  {pool}\n\n"
            f"[b]Commands[/b]\n"
            f"  [b]/[/b] command menu\n"
            f"  [b]/copy[/b] or [b]Ctrl+Y[/b] copy reply/selection\n"
            f"  drag in log = select box text, Ctrl+Y copies\n"
            f"  [b]/model[/b] switch\n"
            f"  [b]/pool[/b] auto candidates\n"
            f"  [b]/agent[/b] build|plan\n"
            f"  [b]/clear[/b] new session\n\n"
            f"[dim]vial {__version__}[/dim]"
        )
        self.query_one("#side", Static).update(panel)
