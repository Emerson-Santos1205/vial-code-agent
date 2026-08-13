"""Widget-based terminal UI for VIAL.

The legacy ANSI UI remains in ``tui.py`` for ``--plain`` and compatibility.
This module uses Textual widgets so focus, resizing, selection and keyboard
events behave like a real terminal application instead of a redrawn screen.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .model import OpenCodeProvider
from .router import RoutingGraph
from .servers import ServerRegistry
from .session import SessionStore
from .tui import COMMANDS, HELP_TEXT, TerminalChatUI, _friendly_model_error


def run_textual_chat(
    root: Path,
    store: SessionStore,
    session_id: str,
    provider: OpenCodeProvider,
    model: str,
    executable: str,
    auto_approve: bool,
    agent: str,
    registry: ServerRegistry | None = None,
) -> int:
    """Run the Textual UI, falling back only when Textual is unavailable."""
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.containers import Horizontal, Vertical
        from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, Static, TextArea
        from textual.worker import Worker, work
    except ImportError:
        from .tui import TerminalChatUI

        return TerminalChatUI(
            root, store, session_id, provider, model, executable,
            auto_approve, agent, registry,
        ).run()

    registry = registry or ServerRegistry(root)

    class ChatApp(App[None]):
        CSS = """
        Screen { background: #090909; color: #e7e7e7; }
        #shell { height: 1fr; }
        #main { width: 1fr; height: 1fr; }
        #messages { height: 1fr; padding: 1 3; overflow-y: auto; }
        #composer { height: auto; padding: 0 3 1 3; }
        #prompt { height: 5; border: round #5b9dff; background: #1b1b1b; }
        #prompt:focus { border: round #75b5ff; }
        #meta { height: 1; padding: 0 1; color: #62a8ff; }
        #hint { height: 1; padding: 0 1; color: #777777; }
        #sidebar { width: 34; background: #151515; padding: 1 2; border-left: solid #252525; }
        #sidebar-title { color: #f0f0f0; text-style: bold; }
        .side-label { color: #888888; margin-top: 1; }
        .side-value { color: #eeeeee; }
        .message { margin: 0 0 1 0; padding: 0 1; }
        .user { color: #75b5ff; }
        .assistant { color: #eeeeee; }
        #palette, #picker { layer: overlay; width: 70%; max-height: 12; background: #202020; border: round #5b9dff; padding: 1; display: none; }
        #palette.visible, #picker.visible { display: block; }
        #palette { dock: bottom; offset: 3 8; }
        #picker { dock: bottom; offset: 3 8; }
        ListItem { padding: 0 1; }
        ListItem.--highlight { background: #28527c; color: white; }
        Header { background: #090909; color: #eeeeee; }
        Footer { background: #101010; }
        """

        BINDINGS = [
            Binding("ctrl+c", "quit", "Quit"),
            Binding("ctrl+l", "clear_messages", "Clear"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.root = root
            self.store = store
            self.session_id = session_id
            self.provider = provider
            self.model = model
            self.executable = executable
            self.auto_approve = auto_approve
            self.agent = agent
            self.registry = registry
            self.routing = RoutingGraph(
                registry, default_model=model, executable=executable,
                auto_approve=auto_approve, agent=agent,
            )
            self.command_delegate = TerminalChatUI(
                root, store, session_id, provider, model, executable,
                auto_approve, agent, registry,
            )
            self.command_items: list[str] = []
            self.model_items: list[str] = []
            self.busy = False

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            with Horizontal(id="shell"):
                with Vertical(id="main"):
                    yield Static(id="messages")
                    with Vertical(id="composer"):
                        yield TextArea(id="prompt", soft_wrap=True)
                        yield Static(self._meta(), id="meta")
                        yield Static("Enter send  Ctrl+J newline  / commands  Ctrl+C exit", id="hint")
                with Vertical(id="sidebar"):
                    yield Static("New session", id="sidebar-title")
                    yield Static(self.session_id[:12], classes="side-value", id="session-value")
                    yield Label("Model", classes="side-label")
                    yield Static(self.model, classes="side-value", id="model-value")
                    yield Label("Status", classes="side-label")
                    yield Static("Ready", classes="side-value", id="status-value")
                    yield Label("Context", classes="side-label")
                    yield Static("Selective workspace", classes="side-value")
                    yield Label("Commands", classes="side-label")
                    yield Static("/  command menu\n/model  model picker\n/clear  new session", classes="side-value")
            yield ListView(id="palette")
            yield ListView(id="picker")
            yield Footer()

        def on_mount(self) -> None:
            self._refresh_messages()
            self.query_one("#prompt", TextArea).focus()

        def _meta(self) -> str:
            return f"Build - {self.model}"

        def _refresh_messages(self) -> None:
            widget = self.query_one("#messages", Static)
            messages = self.store.messages(self.session_id)
            lines: list[str] = []
            for message in messages[-80:]:
                label = "You" if message.role == "user" else "VIAL"
                lines.append(f"[{label}] {message.content}")
            widget.update("\n\n".join(lines))

        def _set_status(self, value: str) -> None:
            self.query_one("#status-value", Static).update(value)

        def _matches(self, value: str) -> list[str]:
            text = value.strip()
            if not text.startswith("/"):
                return []
            matches = [name for name, _ in COMMANDS if name.startswith(text)]
            return sorted(matches, key=lambda name: (name != text, len(name)))[:8]

        def _show_palette(self, matches: list[str]) -> None:
            palette = self.query_one("#palette", ListView)
            palette.clear()
            for command in matches:
                description = dict(COMMANDS).get(command, "")
                palette.append(ListItem(Label(f"{command}  {description}")))
            palette.set_class(bool(matches), "visible")

        def _available_models(self) -> list[str]:
            models = ["auto"]
            for item in self.registry.all_models():
                if item not in models:
                    models.append(item)
            try:
                discovered = self.provider.list_models().strip()
            except (OSError, RuntimeError):
                discovered = ""
            for item in discovered.splitlines():
                item = item.strip()
                if item and item not in models:
                    models.append(item)
            return models

        def _show_picker(self) -> None:
            self.model_items = self._available_models()
            picker = self.query_one("#picker", ListView)
            picker.clear()
            for model_name in self.model_items:
                picker.append(ListItem(Label(model_name)))
            picker.set_class(True, "visible")
            picker.focus()

        def _hide_overlays(self) -> None:
            self.query_one("#palette", ListView).set_class(False, "visible")
            self.query_one("#picker", ListView).set_class(False, "visible")

        def on_text_area_changed(self, event: TextArea.Changed) -> None:
            if event.text_area.id != "prompt" or self.query_one("#picker", ListView).has_class("visible"):
                return
            self.command_items = self._matches(event.text_area.text)
            self._show_palette(self.command_items)

        def on_list_view_selected(self, event: ListView.Selected) -> None:
            if event.list_view.id == "picker":
                index = event.list_view.index or 0
                if self.model_items:
                    self._select_model(self.model_items[index])
                return
            if event.list_view.id == "palette":
                index = event.list_view.index or 0
                if self.command_items:
                    self._complete_command(self.command_items[index])

        def _complete_command(self, command: str) -> None:
            prompt = self.query_one("#prompt", TextArea)
            prompt.text = command + (" " if command in {"/model", "/server", "/pool"} else "")
            prompt.focus()
            self._hide_overlays()
            if command == "/model":
                self._show_picker()

        def _select_model(self, model_name: str) -> None:
            self.model = model_name
            self.provider = OpenCodeProvider(
                model_name, self.executable, self.auto_approve, self.agent)
            self.command_delegate.model = model_name
            self.command_delegate.provider = self.provider
            self.routing.default_model = model_name
            self.query_one("#model-value", Static).update(model_name)
            self.query_one("#meta", Static).update(self._meta())
            self._hide_overlays()
            self.query_one("#prompt", TextArea).focus()

        def on_key(self, event: Any) -> None:
            palette = self.query_one("#palette", ListView)
            if palette.has_class("visible") and event.key in {"up", "down", "tab", "enter"}:
                if event.key == "up":
                    palette.focus()
                    palette.action_cursor_up()
                elif event.key == "down":
                    palette.focus()
                    palette.action_cursor_down()
                elif event.key in {"tab", "enter"} and self.command_items:
                    index = palette.index or 0
                    self._complete_command(self.command_items[index])
                event.stop()
                return
            if event.key == "escape":
                self._hide_overlays()
                self.query_one("#prompt", TextArea).focus()
                event.stop()
                return
            if event.key == "ctrl+j":
                prompt = self.query_one("#prompt", TextArea)
                prompt.insert("\n")
                event.stop()
                return
            if event.key == "enter" and self.query_one("#prompt", TextArea).has_focus:
                self._submit()
                event.stop()

        def _submit(self) -> None:
            prompt = self.query_one("#prompt", TextArea)
            text = prompt.text.strip()
            if not text or self.busy:
                return
            self._hide_overlays()
            prompt.text = ""
            parts = text.split(maxsplit=1)
            if parts and parts[0] == "/model" and len(parts) == 1:
                self._show_picker()
                return
            result = self.handle_command(text)
            if result is not None:
                self._refresh_messages()
                return
            self.store.append(self.session_id, "user", text)
            self._refresh_messages()
            self.busy = True
            self._set_status("Thinking...")
            self._respond(text)

        @work(thread=True)
        def _respond(self, text: str) -> Worker[Any]:
            try:
                response, decision = self.routing.dispatch(
                    text, self.root, requested_model=self.model)
                if response.returncode != 0:
                    output = _friendly_model_error(
                        response.stderr or "model request failed", self.model)
                else:
                    output = response.text.strip()
            except (OSError, RuntimeError) as error:
                output = _friendly_model_error(str(error), self.model)
            self.call_from_thread(self._finish_response, output)

        def _finish_response(self, output: str) -> None:
            self.store.append(self.session_id, "assistant", output)
            self._refresh_messages()
            self.busy = False
            self._set_status("Ready")
            self.query_one("#prompt", TextArea).focus()

        def handle_command(self, text: str) -> bool:
            result = self.command_delegate.handle_command(text)
            if not result.handled:
                return False
            if result.exit:
                self.exit()
                return True
            if result.new_session_id:
                self.session_id = result.new_session_id
                self.command_delegate.session_id = result.new_session_id
                self.command_delegate.store = self.store
            if result.new_model:
                self._select_model(result.new_model)
            if result.output:
                self.store.append(self.session_id, "assistant", result.output)
            self._refresh_messages()
            return True

    ChatApp().run()
    return 0
