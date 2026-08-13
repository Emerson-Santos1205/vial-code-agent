from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .model import OpenCodeProvider
from .router import RoutingGraph
from .session import SessionStore
from .servers import ServerRegistry


@dataclass(frozen=True)
class ChatCommandResult:
    handled: bool
    output: str = ""
    exit: bool = False
    new_session_id: str = ""
    new_model: str = ""


@dataclass(frozen=True)
class InputResult:
    text: str
    cancel: bool = False


COMMANDS = [
    ("/exit", "quit"),
    ("/help", "show help"),
    ("/status", "show session, model, route and pool"),
    ("/clear", "start a new session"),
    ("/sessions", "list past sessions"),
    ("/resume", "resume a past session"),
    ("/models", "list models (registered + opencode)"),
    ("/model", "switch model (auto = route by prompt)"),
    ("/model add", "add a model to a server"),
    ("/model remove", "remove a model from a server"),
    ("/providers", "list opencode providers"),
    ("/servers", "list configured servers"),
    ("/server add", "add an OpenAI-compatible server"),
    ("/server remove", "remove a server"),
    ("/server models", "list a server's models"),
    ("/pool", "show the routing pool"),
    ("/pool add", "add a model to the parallel routing pool"),
    ("/pool remove", "remove a model from the pool"),
]


class TerminalChatUI:
    """Fullscreen terminal chat with in-screen input and history.

    Uses only the standard library. On a real TTY it enters raw key mode and
    supports left/right editing, Backspace, Ctrl+U, Ctrl+L, history with
    Up/Down, scroll with PageUp/PageDown and Ctrl+C/ESC to cancel. If raw mode
    is unavailable it falls back to normal ``input()``.
    """

    def __init__(
        self,
        root: Path,
        store: SessionStore,
        session_id: str,
        provider: OpenCodeProvider,
        model: str,
        executable: str,
        auto_approve: bool,
        agent: str,
        registry: ServerRegistry | None = None,
    ) -> None:
        self.root = root
        self.store = store
        self.session_id = session_id
        self.provider = provider
        self.model = model
        self.executable = executable
        self.auto_approve = auto_approve
        self.agent = agent
        self.registry = registry or ServerRegistry(root)
        self.routing = RoutingGraph(
            self.registry, default_model=model,
            executable=executable, auto_approve=auto_approve, agent=agent,
        )
        self.history: list[str] = []
        self.history_index: int | None = None
        self.scroll = 0
        self.status = "Ready"
        self.route = "auto"
        self._cmd_index = 0
        self._cmd_text = ""

    def run(self) -> int:
        _enable_virtual_terminal()
        self._enter_screen()
        try:
            while True:
                entry = self._read_line()
                if entry.cancel:
                    break
                message = entry.text.strip()
                result = self.handle_command(message)
                if result.new_session_id:
                    self.session_id = result.new_session_id
                    self.scroll = 0
                if result.new_model:
                    self.model = result.new_model
                    self.provider = OpenCodeProvider(
                        self.model, self.executable, self.auto_approve, self.agent)
                    self.routing.default_model = self.model
                if result.exit:
                    break
                if result.handled:
                    if result.output:
                        self.store.append(self.session_id, "assistant", result.output)
                    self.status = "Ready"
                    continue
                if not message:
                    continue
                self.history.append(message)
                self.history_index = None
                self.scroll = 0
                self.store.append(self.session_id, "user", message)
                self.status = "Thinking..."
                self._render(input_buffer="")
                try:
                    response, decision = self.routing.dispatch(
                        message, self.root, requested_model=self.model)
                    self.route = decision.tier
                except (OSError, RuntimeError) as error:
                    self.store.append(self.session_id, "assistant", _friendly_model_error(str(error), self.model))
                    self.status = "Error"
                    continue
                if decision.model:
                    self.route = decision.model
                if response.returncode != 0:
                    error = response.stderr.strip() or f"model exited with code {response.returncode}"
                    self.store.append(self.session_id, "assistant", _friendly_model_error(error, self.model))
                    self.status = "Error"
                    continue
                text = response.text.strip()
                if decision.tier == "deterministic":
                    text = f"[deterministic: {decision.deterministic_keyword}]\n{text}"
                self.store.append(self.session_id, "assistant", text)
                self.status = "Ready"
        finally:
            self._leave_screen()
        return 0

    def handle_command(self, message: str) -> ChatCommandResult:
        command, _, value = message.partition(" ")
        value = value.strip()
        if command == "/exit":
            return ChatCommandResult(True, exit=True)
        if command == "/help":
            return ChatCommandResult(True, HELP_TEXT)
        if command == "/status":
            pool = ", ".join(self.registry.pool) or "empty"
            return ChatCommandResult(
                True,
                f"session: {self.session_id}\n"
                f"model: {self.model}\n"
                f"route: {self.route}\n"
                f"pool: {pool}\n"
                f"messages: {len(self.store.messages(self.session_id))}",
            )
        if command == "/clear":
            return ChatCommandResult(True, "new session", new_session_id=self.store.create())
        if command == "/sessions":
            sessions = self.store.list()
            if not sessions:
                return ChatCommandResult(True, "no sessions")
            return ChatCommandResult(
                True, "sessions (most recent first):\n" + "\n".join(
                    f"  {session_id}  ({len(self.store.messages(session_id))} messages)"
                    for session_id in sessions[:20]
                )
            )
        if command == "/resume":
            if not value:
                return ChatCommandResult(True, "usage: /resume <session_id>")
            try:
                self.store.messages(value)
            except (OSError, FileNotFoundError, json.JSONDecodeError):
                return ChatCommandResult(True, f"unknown session: {value}")
            return ChatCommandResult(
                True, f"resumed session: {value}", new_session_id=value)
        if command == "/servers":
            return ChatCommandResult(True, _servers_output(self.registry))
        if command == "/server":
            return self._handle_server(value)
        if command == "/pool":
            return self._handle_pool(value)
        if command == "/model":
            return self._handle_model_command(value)
        if command == "/models":
            return ChatCommandResult(True, _models_output(self.registry, self.provider, value))
        if command == "/providers":
            try:
                return ChatCommandResult(True, self.provider.list_providers().strip())
            except RuntimeError as error:
                return ChatCommandResult(True, f"error: {error}")
        return ChatCommandResult(False)

    def _handle_server(self, value: str) -> ChatCommandResult:
        parts = value.split()
        if not parts:
            return ChatCommandResult(True, _servers_output(self.registry))
        action = parts[0]
        if action == "add":
            if len(parts) < 3:
                return ChatCommandResult(
                    True,
                    "usage: /server add <name> <base_url> [api_key_env]",
                )
            name = parts[1]
            base_url = parts[2]
            api_key_env = parts[3] if len(parts) > 3 else ""
            try:
                server = self.registry.add_server(name, base_url, api_key_env)
            except ValueError as error:
                return ChatCommandResult(True, f"error: {error}")
            return ChatCommandResult(
                True,
                f"server added: {server.name} ({server.base_url})",
            )
        if action == "remove":
            if len(parts) < 2:
                return ChatCommandResult(True, "usage: /server remove <name>")
            try:
                self.registry.remove_server(parts[1])
            except ValueError as error:
                return ChatCommandResult(True, f"error: {error}")
            return ChatCommandResult(True, f"server removed: {parts[1]}")
        if action == "models":
            if len(parts) < 2:
                return ChatCommandResult(True, "usage: /server models <name>")
            return ChatCommandResult(
                True, _server_models_output(self.registry, parts[1]))
        return ChatCommandResult(True, f"unknown /server action: {action}")

    def _handle_pool(self, value: str) -> ChatCommandResult:
        parts = value.split()
        if not parts:
            return ChatCommandResult(
                True,
                f"pool: {', '.join(self.registry.pool) or 'empty'}",
            )
        action = parts[0]
        if action == "add":
            if len(parts) < 2:
                return ChatCommandResult(True, "usage: /pool add <model_ref>")
            self.registry.pool_add(parts[1])
            return ChatCommandResult(True, f"pool add: {parts[1]}")
        if action == "remove":
            if len(parts) < 2:
                return ChatCommandResult(True, "usage: /pool remove <model_ref>")
            self.registry.pool_remove(parts[1])
            return ChatCommandResult(True, f"pool remove: {parts[1]}")
        return ChatCommandResult(True, f"unknown /pool action: {action}")

    def _handle_model_command(self, value: str) -> ChatCommandResult:
        parts = value.split()
        if not parts:
            return ChatCommandResult(True, "usage: /model [provider/model | add | remove]")
        action = parts[0]
        if action == "add":
            if len(parts) < 2:
                return ChatCommandResult(True, "usage: /model add <server/model>")
            model_ref = parts[1]
            try:
                server_name, model = self.registry.server_and_model(model_ref)
            except ValueError as error:
                return ChatCommandResult(True, f"error: {error}")
            if server_name not in self.registry.servers:
                return ChatCommandResult(
                    True,
                    f"error: unknown server '{server_name}'; use /server add first",
                )
            self.registry.add_model(server_name, model)
            return ChatCommandResult(
                True, f"model added: {model_ref}")
        if action == "remove":
            if len(parts) < 2:
                return ChatCommandResult(True, "usage: /model remove <server/model>")
            model_ref = parts[1]
            try:
                server_name, model = self.registry.server_and_model(model_ref)
            except ValueError as error:
                return ChatCommandResult(True, f"error: {error}")
            self.registry.remove_model(server_name, model)
            return ChatCommandResult(
                True, f"model removed: {model_ref}")
        selected = value
        return ChatCommandResult(True, f"model: {selected}", new_model=selected)

    def _available_models(self) -> list[str]:
        models = ["auto"]
        for model in self.registry.all_models():
            if model not in models:
                models.append(model)
        try:
            discovered = self.provider.list_models().strip()
        except (OSError, RuntimeError):
            discovered = ""
        for model in discovered.splitlines():
            model = model.strip()
            if model and model not in models:
                models.append(model)
        return models

    def _pick_model(self, buffer: list[str], cursor: int) -> InputResult | None:
        models = self._available_models()
        if not models:
            return None
        index = 0
        while True:
            self._render(
                input_buffer="".join(buffer), cursor=cursor,
                picker=("Select model", models), picker_index=index,
            )
            key = _read_key()
            if key == "up":
                index = (index - 1) % len(models)
            elif key == "down":
                index = (index + 1) % len(models)
            elif key in {"enter", "ctrl-m"}:
                return InputResult(f"/model {models[index]}")
            elif key == "esc":
                return None
            elif key == "ctrl-c":
                return InputResult("", cancel=True)

    def _command_matches(self, buffer: list[str]) -> list[str]:
        text = "".join(buffer).strip()
        if not text.startswith("/"):
            return []
        matches = [name for name, _ in COMMANDS if name.startswith(text)]
        return sorted(matches, key=lambda name: (name != text, len(name)))

    def _read_line(self) -> InputResult:
        if not sys.stdin.isatty():
            self._render(input_buffer="")
            try:
                return InputResult(input())
            except EOFError:
                return InputResult("", cancel=True)

        buffer: list[str] = []
        cursor = 0
        self.history_index = None
        self._cmd_index = 0
        self._cmd_text = ""
        while True:
            matches = self._command_matches(buffer)
            self._render(
                input_buffer="".join(buffer), cursor=cursor,
                cmd_matches=matches, cmd_index=self._cmd_index,
            )
            key = _read_key()
            if key.startswith("paste:"):
                pasted = key[len("paste:"):]
                for char in pasted:
                    buffer.insert(cursor, char)
                    cursor += 1
                self._cmd_index = 0
                continue
            if key == "ctrl-c":
                return InputResult("", cancel=True)
            if key == "esc":
                if matches:
                    buffer.clear()
                    cursor = 0
                    self._cmd_index = 0
                continue
            if key == "tab":
                if matches:
                    buffer = list(matches[self._cmd_index % len(matches)])
                    cursor = len(buffer)
                    self._cmd_index = 0
                    continue
                continue
            if key in {"enter", "ctrl-m"}:
                if matches:
                    text = "".join(buffer).strip()
                    selected = matches[self._cmd_index % len(matches)]
                    if text == "/model":
                        picked = self._pick_model(buffer, cursor)
                        if picked is not None:
                            return picked
                        continue
                    if text == selected:
                        return InputResult(selected)
                    if len(text.split()) == 1:
                        buffer = list(selected + " ")
                        cursor = len(buffer)
                        self._cmd_index = 0
                        continue
                return InputResult("".join(buffer))
            if key == "newline":
                buffer.insert(cursor, "\n")
                cursor += 1
                continue
            if key == "ctrl-l":
                self.scroll = 0
                continue
            if key == "ctrl-u":
                buffer.clear()
                cursor = 0
                self._cmd_index = 0
                continue
            if key == "backspace":
                if cursor > 0:
                    del buffer[cursor - 1]
                    cursor -= 1
                self._cmd_index = 0
                continue
            if key == "delete":
                if cursor < len(buffer):
                    del buffer[cursor]
                self._cmd_index = 0
                continue
            if key == "left":
                cursor = max(0, cursor - 1)
                continue
            if key == "right":
                cursor = min(len(buffer), cursor + 1)
                continue
            if key == "home":
                cursor = 0
                continue
            if key == "end":
                cursor = len(buffer)
                continue
            if key == "pageup":
                self.scroll += 5
                continue
            if key == "pagedown":
                self.scroll = max(0, self.scroll - 5)
                continue
            if key == "up":
                if matches:
                    self._cmd_index = (self._cmd_index - 1) % len(matches)
                    continue
                recalled = self._history(-1)
                if recalled is not None:
                    buffer = list(recalled)
                    cursor = len(buffer)
                continue
            if key == "down":
                if matches:
                    self._cmd_index = (self._cmd_index + 1) % len(matches)
                    continue
                recalled = self._history(1)
                buffer = list(recalled or "")
                cursor = len(buffer)
                continue
            if len(key) == 1 and key >= " ":
                buffer.insert(cursor, key)
                cursor += 1
                self._cmd_index = 0
                continue

    def _history(self, direction: int) -> str | None:
        if not self.history:
            return None
        if self.history_index is None:
            self.history_index = len(self.history) if direction < 0 else None
        if self.history_index is None:
            return None
        self.history_index = max(0, min(len(self.history), self.history_index + direction))
        if self.history_index == len(self.history):
            return ""
        return self.history[self.history_index]

    def _render(
        self, input_buffer: str, cursor: int | None = None,
        cmd_matches: list[str] | None = None, cmd_index: int = 0,
        picker: tuple[str, list[str]] | None = None, picker_index: int = 0,
    ) -> None:
        width, height = shutil.get_terminal_size((100, 32))
        messages = self.store.messages(self.session_id)
        matches = cmd_matches if cmd_matches is not None else []
        if not messages:
            lines, cursor_row, cursor_col = _landing_screen(
                width, height, input_buffer, cursor, self.model,
                matches, cmd_index, picker, picker_index,
            )
        else:
            lines, cursor_row, cursor_col = _workspace_screen(
                width, height, messages, input_buffer, cursor,
                self.session_id, self.model, self.status, self.scroll,
                matches, cmd_index, picker, picker_index,
            )
        sys.stdout.write("\n".join(lines))
        if cursor_row is not None and cursor_col is not None:
            sys.stdout.write(f"\x1b[{cursor_row};{cursor_col}H")
        sys.stdout.flush()

    @staticmethod
    def _enter_screen() -> None:
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?2004h")
        sys.stdout.flush()

    @staticmethod
    def _leave_screen() -> None:
        sys.stdout.write("\x1b[?2004l\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()


def run_plain_chat(
    root: Path,
    store: SessionStore,
    session_id: str,
    provider: OpenCodeProvider,
    model: str,
    executable: str,
    auto_approve: bool,
    agent: str,
) -> int:
    print(f"session: {session_id}")
    print("Digite /help para comandos ou /exit para sair.")
    ui = TerminalChatUI(root, store, session_id, provider, model, executable, auto_approve, agent)
    while True:
        try:
            message = input("you> ")
        except EOFError:
            break
        result = ui.handle_command(message.strip())
        if result.new_session_id:
            ui.session_id = result.new_session_id
            session_id = result.new_session_id
        if result.new_model:
            ui.model = result.new_model
            ui.provider = OpenCodeProvider(result.new_model, executable, auto_approve, agent)
        if result.exit:
            break
        if result.handled:
            if result.output:
                print(result.output)
            continue
        if not message.strip():
            continue
        store.append(session_id, "user", message)
        try:
            response, decision = ui.routing.dispatch(message, root, requested_model=ui.model)
        except (OSError, RuntimeError) as error:
            print(_friendly_model_error(str(error), ui.model))
            continue
        if response.returncode != 0:
            error = response.stderr.strip() or f"model exited with code {response.returncode}"
            print(_friendly_model_error(error, ui.model))
            continue
        text = response.text.strip()
        if decision.tier == "deterministic":
            text = f"[deterministic: {decision.deterministic_keyword}]\n{text}"
        store.append(session_id, "assistant", text)
        print(f"assistant> {text}")
    return 0


def _servers_output(registry) -> str:
    servers = registry.list_servers()
    if not servers:
        return "no servers configured\n\nusage: /server add <name> <base_url> [api_key_env]"
    lines = [f"{len(servers)} server(s):"]
    for server in servers:
        models = ", ".join(server.models) or "(none yet)"
        key = server.api_key_env or "no key"
        lines.append(f"  {server.name}  {server.base_url}  key={key}")
        lines.append(f"    models: {models}")
    return "\n".join(lines)


def _server_models_output(registry, name: str) -> str:
    server = registry.servers.get(name)
    if server is None:
        return f"unknown server: {name}"
    lines = [f"{server.name} models:"]
    if not server.models:
        lines.append("  (none; add with /model add <server>/<model>)")
    for model in server.models:
        lines.append(f"  {server.name}/{model}")
    return "\n".join(lines)


def _models_output(registry, provider, provider_filter: str) -> str:
    parts: list[str] = []
    registry_models = registry.all_models()
    if registry_models:
        parts.append("registered:")
        parts.extend(f"  {ref}" for ref in registry_models)
    try:
        discovered = provider.list_models(provider_filter or None).strip()
    except (OSError, RuntimeError) as error:
        discovered = ""
        parts.append(f"discovery error: {error}")
    if discovered:
        if parts:
            parts.append("opencode:")
        parts.append(discovered)
    if not registry_models and not discovered:
        return "no models available"
    return "\n".join(parts)


def _landing_screen(
    width: int, height: int, input_buffer: str, cursor: int | None, model: str,
    cmd_matches: list[str] | None = None, cmd_index: int = 0,
    picker: tuple[str, list[str]] | None = None, picker_index: int = 0,
) -> tuple[list[str], int | None, int | None]:
    side_width = 40 if width >= 100 else 0
    main_width = width - side_width
    prompt_width = min(78, max(56, main_width - 18))
    left = max(2, (main_width - prompt_width) // 2)
    input_lines = _wrap_input(input_buffer, prompt_width)
    box_height = max(3, len(input_lines) + 2)
    cursor_row = None
    cursor_col = None
    lines = ["\x1b[2J\x1b[H"] + ["" for _ in range(height - 1)]

    palette = _picker_lines(*picker, picker_index, prompt_width) if picker else _command_palette(cmd_matches, cmd_index, prompt_width)
    logo = _wordmark()
    stack_height = len(logo) + 3 + len(palette) + box_height + 3
    top = max(1, (height - stack_height) // 2)
    side = _opencode_side_panel("", model, "Ready", height, side_width)

    for index, logo_line in enumerate(logo):
        _put(lines, top + index, _center(logo_line, main_width))
    prompt_top = top + len(logo) + 3
    palette_top = prompt_top - len(palette)
    for index, line in enumerate(palette):
        _put(lines, top + index, " " * left + logo_line)
    for index, line in enumerate(palette):
        _put(lines, palette_top + index, " " * left + line)
    composer_top = prompt_top
    composer = _input_box_lines(input_buffer, prompt_width)
    for index, line in enumerate(composer):
        _put(lines, composer_top + index, " " * left + line)
    for index, right in enumerate(side):
        left_line = "" if index == 0 else lines[index]
        _put(lines, index, _compose_row(left_line, right, main_width, side_width))
    _put(lines, height - 2, " " * max(0, width - 9) + MUTED + __version__ + RESET)
    _put(lines, height - 3, " " * 2 + MUTED + "~" + RESET)
    if cursor is not None:
        row, col = _cursor_position(input_buffer, cursor, prompt_width)
        cursor_row = composer_top + row
        cursor_col = left + 3 + col
    return lines, cursor_row, cursor_col


def _workspace_screen(
    width: int,
    height: int,
    messages,
    input_buffer: str,
    cursor: int | None,
    session_id: str,
    model: str,
    status: str,
    scroll: int,
    cmd_matches: list[str] | None = None,
    cmd_index: int = 0,
    picker: tuple[str, list[str]] | None = None,
    picker_index: int = 0,
) -> tuple[list[str], int | None, int | None]:
    side_width = 40 if width >= 100 else 0
    main_width = width - side_width
    palette = _picker_lines(*picker, picker_index, main_width) if picker else _command_palette(cmd_matches, cmd_index, main_width)
    input_lines = _wrap_input(input_buffer, main_width)
    composer_height = max(5, len(input_lines) + 4)
    body_height = max(5, height - composer_height - len(palette) - 1)
    body = _render_workspace_messages(messages, main_width, body_height, scroll)
    side = _opencode_side_panel(session_id, model, status, height, side_width)
    lines: list[str] = ["\x1b[2J\x1b[H"]
    for index in range(body_height):
        right = side[index] if index < len(side) else ""
        lines.append(_compose_row("   " + body[index], right, main_width, side_width))
    for index, line in enumerate(palette):
        side_index = body_height + index
        right = side[side_index] if side_index < len(side) else ""
        lines.append(_compose_row("   " + line, right, main_width, side_width))
    composer = _input_box_lines(input_buffer, main_width)
    for index, line in enumerate(composer, start=body_height + len(palette)):
        right = side[index] if index < len(side) else ""
        lines.append(_compose_row("   " + line, right, main_width, side_width))
    while len(lines) < height:
        index = len(lines)
        right = side[index] if index < len(side) else ""
        lines.append(_compose_row("", right, main_width, side_width))
    cursor_row = None
    cursor_col = None
    if cursor is not None:
        row, col = _cursor_position(input_buffer, cursor, main_width)
        cursor_row = body_height + len(palette) + 1 + row
        cursor_col = 3 + col
    return lines[:height], cursor_row, cursor_col


def _header_bar(model: str, session_id: str, status: str, width: int) -> str:
    left = f"  {TEXT}vial{RESET} {MUTED}·{RESET} {BLUE}Build{RESET}"
    right = (
        f"{MUTED}model:{RESET} {TEXT}{_clip(model, 24)}{RESET}  "
        f"{MUTED}{session_id[:8]}{RESET}  {TEXT}{status}{RESET}  "
    )
    gap = max(1, width - _visible_len(left) - _visible_len(right))
    return BG + (left + " " * gap + right)[:width].ljust(width) + RESET


def _command_palette(matches: list[str] | None, index: int, width: int) -> list[str]:
    """Compact OpenCode-style command picker above the composer."""
    if not matches:
        return []
    heading = "  Commands  "
    hint = "Enter send · ↑↓ select · Tab complete"
    palette: list[str] = [
        (heading + MUTED + hint + RESET)[:width].ljust(width)
    ]
    for item_index, name in enumerate(matches[:8]):
        description = dict(COMMANDS).get(name, "")
        label = f"  {name}  {description}"
        if item_index == index % len(matches[:8]):
            palette.append(BG_SELECT + label[:width].ljust(width) + RESET)
        else:
            palette.append((BLUE + f"  {name}" + RESET + f"  {MUTED}{description}{RESET}")[:width].ljust(width))
    return [line[:width].ljust(width) for line in palette]


def _picker_lines(title: str, items: list[str], index: int, width: int) -> list[str]:
    """Render a bordered selection popup above the composer."""
    if not items:
        return []
    inner = max(20, width - 2)
    visible = items[:8]
    heading = f"  {title} "
    lines = ["+" + "-" * (width - 2) + "+"]
    lines[0] = (heading + "-" * max(0, width - len(heading) - 2) + "+")[:width]
    for item_index, item in enumerate(visible):
        label = f" {'> ' if item_index == index % len(visible) else '  '}{item}"
        label = label[:inner].ljust(inner)
        if item_index == index % len(visible):
            lines.append("|" + BG_SELECT + label + RESET + "|")
        else:
            lines.append("|" + label + "|")
    lines.append("+" + "-" * (width - 2) + "+")
    return lines


def _render_workspace_messages(messages, width: int, max_lines: int, scroll: int) -> list[str]:
    rendered: list[str] = []
    for message in messages[-80:]:
        if message.role == "user":
            rendered.extend(_user_message(message.content.strip(), width))
        else:
            rendered.extend(_assistant_message(message.content.strip(), width))
        rendered.append("")
    end = max(0, len(rendered) - scroll)
    start = max(0, end - max_lines)
    window = rendered[start:end]
    return window + [""] * max(0, max_lines - len(window))


def _user_message(text: str, width: int) -> list[str]:
    """Render a compact user turn with the OpenCode composer treatment."""
    inner = max(20, width - 8)
    wrapped = _wrap_text(text or " ", inner)
    lines = [BLUE + "You" + RESET + " " + wrapped[0]]
    for line in wrapped[1:]:
        lines.append("  " + line)
    return lines


def _assistant_message(text: str, width: int) -> list[str]:
    """Render one assistant turn without duplicating its first line."""
    inner = max(20, width - 8)
    wrapped = _wrap_text(text or " ", inner)
    lines = [MUTED + "VIAL" + RESET + " " + wrapped[0]]
    for line in wrapped[1:]:
        lines.append("  " + TEXT + line[:max(0, width - 4)] + RESET)
    return lines


def _workspace_prompt_box(input_buffer: str, width: int, input_lines: list[str]) -> str:
    return _input_box_lines(input_buffer, width)[0]


def _input_box_lines(value: str, width: int) -> list[str]:
    inner = max(10, width - 2)
    text_width = max(10, width - 8)
    lines = _wrap_text(value, text_width) if value else [""]
    rendered = [PANEL_BG + "+" + "-" * inner + "+" + RESET]
    if not value:
        placeholder = MUTED + 'Ask anything... "Fix a TODO in the codebase"' + RESET
        rendered.append(PANEL_BG + "| " + BLUE + "> " + RESET + placeholder + " " * max(0, width - _visible_len(placeholder) - 5) + " |" + RESET)
    else:
        for index, line in enumerate(lines):
            prefix = BLUE + "> " + RESET if index == 0 else "  "
            content = prefix + line
            rendered.append(PANEL_BG + "| " + _pad_visible(content, width - 4) + " |" + RESET)
    rendered.append(PANEL_BG + "+" + "-" * inner + "+" + RESET)
    meta = "  " + BLUE + "Build" + RESET + " " + MUTED + "-" + RESET + " " + TEXT + "auto-routing" + RESET
    rendered.append(meta[:width])
    rendered.append(MUTED + "  Enter send - Ctrl+J newline - / commands - Ctrl+C exit" + RESET)
    return rendered


def _opencode_side_panel(session_id: str, model: str, status: str, height: int, width: int) -> list[str]:
    if width <= 0:
        return []
    lines = [
        "",
        f"  {TEXT}New session{RESET}",
        f"  {MUTED}{session_id[:12]}{RESET}",
        "",
        f"  {MUTED}Model{RESET}",
        f"  {TEXT}{_clip(model, width - 3)}{RESET}",
        "",
        f"  {MUTED}Status{RESET}",
        f"  {TEXT}{status}{RESET}",
        "",
        f"  {MUTED}Context{RESET}",
        f"  {MUTED}selective workspace{RESET}",
        "",
        f"  {MUTED}Commands{RESET}",
        f"  {BLUE}/{RESET} command menu",
        f"  {BLUE}/model{RESET} switch",
        f"  {BLUE}/clear{RESET} new session",
    ]
    filler = max(0, height - len(lines) - 3)
    lines.extend([""] * filler)
    lines.extend([
        f"  {GREEN}*{RESET} {TEXT}VIAL{RESET} {MUTED}{__version__}{RESET}",
    ])
    return [(SIDE_BG + line[:width].ljust(width) + RESET) for line in lines[:height]]


def _prompt_top(width: int) -> str:
    return BLUE + "|" + RESET + PANEL_BG + " " * max(0, width - 1) + RESET


def _prompt_line(text: str, width: int, active: bool = False) -> str:
    marker = BLUE + "|" + RESET if active else " "
    visible = _visible_len(text)
    content_width = max(0, width - 2)
    content = " " + text[:content_width - 1] + " " * max(0, content_width - 1 - visible)
    return marker + PANEL_BG + content[:content_width] + RESET


def _wordmark() -> list[str]:
    return [
        f"{MUTED}oooo  ppp  eeee nn  ccc  oooo dddd eeee{RESET}",
        f"{MUTED}o  o  p  p e    nnn c    o  o d  d e{RESET}",
        f"{TEXT}o  o  ppp  eee  n n c    o  o d  d eee{RESET}",
        f"{TEXT}oooo  p    eeee n  n ccc  oooo dddd eeee{RESET}",
    ]


def _center(text: str, width: int) -> str:
    visible = _visible_len(text)
    return " " * max(0, (width - visible) // 2) + text


def _put(lines: list[str], row: int, value: str) -> None:
    if 0 <= row < len(lines):
        lines[row] = value


def _wrap_text(value: str, width: int) -> list[str]:
    result: list[str] = []
    for part in value.split("\n"):
        result.extend(textwrap.wrap(part, width, replace_whitespace=False, break_long_words=False) or [""])
    return result or [""]


def _wrap_input(value: str, width: int) -> list[str]:
    inner = max(10, width - 8)
    if not value:
        return [""]
    lines: list[str] = []
    for part in value.split("\n"):
        lines.extend(
            textwrap.wrap(part, inner, replace_whitespace=False, drop_whitespace=False)
            or [""]
        )
    return lines or [""]


def _cursor_position(value: str, cursor: int, width: int) -> tuple[int, int]:
    inner = max(10, width - 8)
    before = value[:cursor]
    row = 1
    col_offset = 0
    for part in before.split("\n")[:-1]:
        row += max(1, (len(part) + inner - 1) // inner)
    last = before.split("\n")[-1]
    row += len(last) // inner
    col_offset = len(last) % inner
    col = 8 + col_offset
    return row, col


def _main_width(width: int) -> int:
    if width < 92:
        return width
    return max(52, width - _side_width(width))


def _side_width(width: int) -> int:
    if width < 92:
        return 0
    return min(34, max(28, width // 3))


def _top_brand(width: int) -> str:
    brand = f" {MUTED}v{RESET}{TEXT}ial{RESET}"
    title = "  Build"
    text = brand + title
    return BG + text + " " * max(0, width - _visible_len(text)) + RESET


def _web_hint(width: int) -> str:
    text = "  Ask VIAL to inspect, explain, or change the workspace.  /help for commands"
    return MUTED + text[:width].ljust(width) + RESET


def _compose_row(left: str, right: str, main_width: int, side_width: int) -> str:
    if side_width <= 0:
        return left[:main_width].ljust(main_width)
    return left[:main_width].ljust(main_width) + SIDE_BG + right[:side_width].ljust(side_width) + RESET


def _composer_bar(width: int) -> str:
    label = " Enter send  Ctrl+J newline  paste supported  Ctrl+U clear  Ctrl+C/ESC exit "
    return BLUE_BG + label[:width].ljust(width) + RESET


def _side_blank(width: int) -> str:
    if width <= 0:
        return ""
    return SIDE_BG + " " * width + RESET


def _side_panel(session_id: str, model: str, status: str, height: int, width: int) -> list[str]:
    if width <= 0:
        return []
    items = [
        "",
        f" {TEXT}New session{RESET}",
        "",
        f" {MUTED}Model{RESET}",
        f" {TEXT}{_clip(model, width - 2)}{RESET}",
        "",
        f" {MUTED}Context{RESET}",
        f" {TEXT}Selective workspace context{RESET}",
        "",
        f" {MUTED}VIAL{RESET}",
        f" {TEXT}Organization cognitive runtime{RESET}",
        "",
        f" {MUTED}Resource{RESET}",
        f" {TEXT}OpenCode execution resource{RESET}",
        "",
        f" {MUTED}Session{RESET}",
        f" {TEXT}{session_id[:12]}{RESET}",
        "",
        f" {MUTED}Status{RESET}",
        f" {TEXT}{status}{RESET}",
        "",
        f" {BLUE}/clear{RESET} new session",
        f" {BLUE}/model{RESET} switch model",
        f" {BLUE}/exit{RESET} quit",
    ]
    return items[:height] + [""] * max(0, height - len(items))


def _message_line(text: str, width: int, user: bool) -> str:
    marker = BLUE if user else MUTED
    return marker + "|" + RESET + " " + text[:max(0, width - 3)]


def _clip(value: str, width: int) -> str:
    return value if len(value) <= width else value[:max(0, width - 3)] + "..."


def _pad_visible(value: str, width: int) -> str:
    """Pad an ANSI-colored line to a terminal width."""
    visible = _visible_len(value)
    return value[:max(0, width)] + " " * max(0, width - visible)


def _visible_len(text: str) -> int:
    visible = 0
    in_escape = False
    for char in text:
        if char == "\x1b":
            in_escape = True
            continue
        if in_escape:
            if char == "m":
                in_escape = False
            continue
        visible += 1
    return visible


def _friendly_model_error(error: str, model: str) -> str:
    lowered = error.lower()
    if "cannot connect" in lowered or "unable to connect" in lowered:
        return (
            "Cannot connect to the selected model provider.\n"
            f"model: {model}\n"
            "Try one of these:\n"
            "1. /models and then /model provider/model\n"
            "2. vial providers to verify credentials\n"
            "3. choose a free/local model such as opencode/deepseek-v4-flash-free"
        )
    return f"error: {error}"


RESET = "\x1b[0m"
TEXT = "\x1b[38;5;255m"
MUTED = "\x1b[38;5;245m"
BLUE = "\x1b[38;5;75m"
GREEN = "\x1b[38;5;77m"
BG = "\x1b[48;5;232m"
SIDE_BG = "\x1b[48;5;233m"
PANEL_BG = "\x1b[48;5;234m"
USER_BG = "\x1b[48;5;236m"
BG_SELECT = "\x1b[48;5;24m\x1b[38;5;231m"
BLUE_BG = "\x1b[48;5;24m\x1b[38;5;231m"


def _read_key() -> str:
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in {"\x00", "\xe0"}:
            code = msvcrt.getwch()
            return WINDOWS_KEYS.get(code, "")
        return KEY_ALIASES.get(ch, ch)
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            seq = ch + sys.stdin.read(1)
            if seq == "\x1b[":
                third = sys.stdin.read(1)
                if third == "2":
                    marker = sys.stdin.read(4)
                    if marker == "00~":
                        return "paste:" + _read_bracketed_paste()
                    return ""
                if third in {"1", "3", "4", "5", "6"}:
                    sys.stdin.read(1)
                    return ANSI_KEYS.get(third, "")
                return ANSI_KEYS.get(third, "esc")
            return "esc"
        return KEY_ALIASES.get(ch, ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_bracketed_paste() -> str:
    data: list[str] = []
    suffix = ""
    while True:
        char = sys.stdin.read(1)
        data.append(char)
        suffix = (suffix + char)[-6:]
        if suffix == "\x1b[201~":
            return "".join(data)[:-6]


def _enable_virtual_terminal() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


KEY_ALIASES = {
    "\r": "enter",
    "\n": "enter",
    "\x03": "ctrl-c",
    "\x0c": "ctrl-l",
    "\x15": "ctrl-u",
    "\x1b": "esc",
    "\x7f": "backspace",
    "\b": "backspace",
}

WINDOWS_KEYS = {
    "H": "up",
    "P": "down",
    "K": "left",
    "M": "right",
    "G": "home",
    "O": "end",
    "S": "delete",
    "I": "pageup",
    "Q": "pagedown",
}

ANSI_KEYS = {
    "A": "up",
    "B": "down",
    "C": "right",
    "D": "left",
    "H": "home",
    "F": "end",
    "1": "home",
    "3": "delete",
    "4": "end",
    "5": "pageup",
    "6": "pagedown",
}

HELP_TEXT = """Commands:
/models [provider]            list models (registered + opencode)
/model provider/model         switch model (auto = route by prompt)
/model add server/model       add a model to a server
/model remove server/model    remove a model from a server
/providers                    list opencode providers
/servers                      list configured servers
/server add <name> <base_url> [api_key_env]   add an OpenAI-compatible server
/server remove <name>         remove a server
/server models <name>         list a server's models
/pool                         show the routing pool
/pool add <model_ref>         add a model to the parallel routing pool
/pool remove <model_ref>      remove a model from the pool
/status                       show session status
/sessions                     list past sessions
/resume <session_id>          resume a past session
/clear                        start a new session
/exit                         quit
""".strip()
