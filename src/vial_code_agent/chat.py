"""Framework-free chat controller for the opencode-style terminal UI.

The controller owns the conversation state (session, model, agent, routing
pool) and every slash command. It has no dependency on Textual/Rich so the
command handling stays unit-testable; the terminal UI in ``app.py`` only
renders the results.

Routing is orchestrator-driven: ``auto`` lets ``RoutingGraph`` pick a tier and
dispatch the prompt to every candidate model in the pool (first valid response
wins), while ``/model <provider>/<model>`` pins a single model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .model import OpenCodeProvider
from .router import RoutingGraph
from .servers import ServerRegistry
from .session import SessionStore


@dataclass(frozen=True)
class ChatCommandResult:
    handled: bool
    output: str = ""
    exit: bool = False
    new_session_id: str = ""
    new_model: str = ""
    new_agent: str = ""
    clipboard: str = ""


COMMANDS = [
    ("/exit", "quit"),
    ("/help", "show help"),
    ("/status", "show session, model, agent, route and pool"),
    ("/trace", "show the audit trail for a decision (/trace <id>)"),
    ("/approve", "approve a pending decision (/approve <id>)"),
    ("/decisions", "list decisions awaiting consensus or approval"),
    ("/consensus", "show consensus status of pending decisions"),
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
    ("/pool set", "replace the pool with exactly these models"),
    ("/events", "list recent ΔState events (/events [event_id])"),
    ("/delta", "capture/compare the materialized project state"),
    ("/agent", "switch agent: build or plan"),
    ("/auto", "toggle auto-approval of workspace permissions"),
    ("/copy", "copy the last assistant response to the clipboard"),
]


class ChatController:
    """State and command handling for a VIAL chat session."""

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
        runtime=None,
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
        self.runtime = runtime
        self.routing = RoutingGraph(
            self.registry, default_model=model,
            executable=executable, auto_approve=auto_approve, agent=agent,
        )
        self.route = "auto"
        self.history: list[str] = []

    # ------------------------------------------------------------------ #
    # Command handling
    # ------------------------------------------------------------------ #
    def handle(self, message: str) -> ChatCommandResult:
        command, _, value = message.partition(" ")
        value = value.strip()
        if command == "/exit":
            return ChatCommandResult(True, exit=True)
        if command == "/help":
            return ChatCommandResult(True, HELP_TEXT)
        if command == "/status":
            return ChatCommandResult(True, self._status_output())
        if command == "/trace":
            return self._handle_trace(value)
        if command == "/approve":
            return self._handle_approve(value)
        if command == "/decisions":
            return self._handle_decisions(value)
        if command == "/consensus":
            return self._handle_consensus(value)
        if command == "/clear":
            return ChatCommandResult(True, "new session", new_session_id=self.store.create())
        if command == "/sessions":
            return self._handle_sessions()
        if command == "/resume":
            return self._handle_resume(value)
        if command == "/servers":
            return ChatCommandResult(True, _servers_output(self.registry))
        if command == "/server":
            return self._handle_server(value)
        if command == "/pool":
            return self._handle_pool(value)
        if command == "/events":
            return self._handle_events(value)
        if command == "/delta":
            return self._handle_delta(value)
        if command == "/model":
            return self._handle_model_command(value)
        if command == "/models":
            return ChatCommandResult(True, _models_output(self.registry, self.provider, value))
        if command == "/providers":
            try:
                return ChatCommandResult(True, self.provider.list_providers().strip())
            except RuntimeError as error:
                return ChatCommandResult(True, f"error: {error}")
        if command == "/agent":
            return self._handle_agent(value)
        if command == "/auto":
            self.auto_approve = not self.auto_approve
            self.routing.auto_approve = self.auto_approve
            return ChatCommandResult(True, f"auto-approve: {'on' if self.auto_approve else 'off'}")
        if command == "/copy":
            return self._handle_copy()
        return ChatCommandResult(False)

    def respond(self, message: str) -> tuple[str, str]:
        """Dispatch a prompt through the router and return (text, route)."""
        self.history.append(message)
        self.store.append(self.session_id, "user", message)
        response, decision = self.routing.dispatch(
            message, self.root, requested_model=self.model,
            history=self._prior_turns())
        self.route = decision.model or decision.tier
        if response.returncode != 0:
            error = response.stderr.strip() or f"model exited with code {response.returncode}"
            text = _friendly_model_error(error, self.model)
            self.store.append(self.session_id, "assistant", text)
            return text, self.route
        text = response.text.strip()
        if decision.tier == "deterministic":
            text = f"[deterministic: {decision.deterministic_keyword}]\n{text}"
        self.store.append(self.session_id, "assistant", text)
        return text, self.route

    def respond_stream(self, message: str):
        """Yield text chunks of the response; persists the turns on completion.

        Complements :meth:`respond` for the TUI: the assistant text is written
        to the session store once the stream finishes (same durable behaviour).
        """
        self.history.append(message)
        self.store.append(self.session_id, "user", message)
        self.route = self.routing.model_for(message, self.model) or self.route
        chunks: list[str] = []
        for chunk in self.routing.dispatch_stream(
            message, self.root, requested_model=self.model,
            history=self._prior_turns()):
            chunks.append(chunk)
            yield chunk
        text = "".join(chunks).strip()
        self.store.append(self.session_id, "assistant", text)

    def cancel_stream(self) -> None:
        """Terminate any model subprocess currently streaming."""
        self.routing.cancel_active()

    def _prior_turns(self, limit: int = 20) -> list[tuple[str, str]]:
        """Recent ``(role, content)`` turns of the current session.

        The just-appended user prompt is excluded so it remains the request the
        model answers. Feeding these turns back keeps follow-up prompts (for
        example "translate the answer above to Portuguese") in context instead
        of starting a fresh conversation.
        """
        try:
            messages = self.store.messages(self.session_id)
        except (OSError, FileNotFoundError, json.JSONDecodeError):
            return []
        prior = messages[:-1] if messages else []
        turns: list[tuple[str, str]] = []
        for message in prior[-limit:]:
            role = "user" if message.role == "user" else "assistant"
            turns.append((role, message.content))
        return turns

    # ------------------------------------------------------------------ #
    # Slash command internals
    # ------------------------------------------------------------------ #
    def _status_output(self) -> str:
        pool = ", ".join(self.registry.pool) or "empty"
        if self.model and self.model != "auto":
            routing = f"pinned ({self.model})"
        else:
            routing = f"auto (orchestrator · {len(self.registry.pool)} candidate(s) in pool)"
        return (
            f"session: {self.session_id}\n"
            f"model: {self.model}\n"
            f"routing: {routing}\n"
            f"agent: {self.agent}\n"
            f"route: {self.route}\n"
            f"pool: {pool}\n"
            f"auto-approve: {'on' if self.auto_approve else 'off'}\n"
            f"messages: {len(self.store.messages(self.session_id))}"
        )

    def _handle_sessions(self) -> ChatCommandResult:
        sessions = self.store.list()
        if not sessions:
            return ChatCommandResult(True, "no sessions")
        return ChatCommandResult(
            True, "sessions (most recent first):\n" + "\n".join(
                f"  {session_id}  ({len(self.store.messages(session_id))} messages)"
                for session_id in sessions[:20]
            )
        )

    def _handle_resume(self, value: str) -> ChatCommandResult:
        if not value:
            return ChatCommandResult(True, "usage: /resume <session_id>")
        try:
            self.store.messages(value)
        except (OSError, FileNotFoundError, json.JSONDecodeError):
            return ChatCommandResult(True, f"unknown session: {value}")
        return ChatCommandResult(True, f"resumed session: {value}", new_session_id=value)

    def _handle_server(self, value: str) -> ChatCommandResult:
        parts = value.split()
        if not parts:
            return ChatCommandResult(True, _servers_output(self.registry))
        action = parts[0]
        if action == "add":
            if len(parts) < 3:
                return ChatCommandResult(
                    True, "usage: /server add <name> <base_url> [api_key_env]")
            try:
                server = self.registry.add_server(parts[1], parts[2], parts[3] if len(parts) > 3 else "")
            except ValueError as error:
                return ChatCommandResult(True, f"error: {error}")
            return ChatCommandResult(
                True, f"server added: {server.name} ({server.base_url})")
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
            return ChatCommandResult(True, _server_models_output(self.registry, parts[1]))
        return ChatCommandResult(True, f"unknown /server action: {action}")

    def _handle_pool(self, value: str) -> ChatCommandResult:
        parts = value.split()
        if not parts:
            return ChatCommandResult(
                True, f"pool: {', '.join(self.registry.pool) or 'empty'}")
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
        if action == "set":
            refs = parts[1:]
            if not refs:
                return ChatCommandResult(
                    True, "usage: /pool set <model_ref> [model_ref ...]")
            self.registry.pool_set(refs)
            return ChatCommandResult(
                True, f"pool set: {', '.join(refs)}")
        return ChatCommandResult(True, f"unknown /pool action: {action}")

    def _handle_model_command(self, value: str) -> ChatCommandResult:
        parts = value.split()
        if not parts:
            return ChatCommandResult(True, "usage: /model [provider/model | add | remove]")
        action = parts[0]
        if action == "add":
            if len(parts) < 2:
                return ChatCommandResult(True, "usage: /model add <server/model>")
            try:
                server_name, model = self.registry.server_and_model(parts[1])
            except ValueError as error:
                return ChatCommandResult(True, f"error: {error}")
            if server_name not in self.registry.servers:
                return ChatCommandResult(
                    True, f"error: unknown server '{server_name}'; use /server add first")
            self.registry.add_model(server_name, model)
            return ChatCommandResult(True, f"model added: {parts[1]}")
        if action == "remove":
            if len(parts) < 2:
                return ChatCommandResult(True, "usage: /model remove <server/model>")
            try:
                server_name, model = self.registry.server_and_model(parts[1])
            except ValueError as error:
                return ChatCommandResult(True, f"error: {error}")
            self.registry.remove_model(server_name, model)
            return ChatCommandResult(True, f"model removed: {parts[1]}")
        selected = value
        pinned = "auto (orchestrator -> pool)" if selected == "auto" else f"{selected} (pinned, pool inactive)"
        return ChatCommandResult(True, f"model: {pinned}", new_model=selected)

    def _handle_events(self, value: str) -> ChatCommandResult:
        if self.runtime is None:
            return ChatCommandResult(True, "governed runtime unavailable; pass --vial-root")
        events = self.runtime.event_delta(after_event_id=value.strip())
        if not events:
            return ChatCommandResult(True, "no events")
        lines = [
            f"{event.type} {event.resource} v{event.version} actor={event.actor} {event.data}"
            for event in events[-20:]
        ]
        return ChatCommandResult(True, "\n".join(lines))

    def _handle_delta(self, value: str) -> ChatCommandResult:
        if self.runtime is None:
            return ChatCommandResult(True, "governed runtime unavailable; pass --vial-root")
        from .workspace import select_files
        files = select_files(
            self.root, ["*.py"], [".git", ".venv", "__pycache__"])
        delta = self.runtime.project_delta(self.root, files)
        if delta is None:
            return ChatCommandResult(True, "baseline captured; run again for a delta")
        return ChatCommandResult(
            True, json.dumps(delta.to_dict(), indent=2, ensure_ascii=False))

    def _handle_agent(self, value: str) -> ChatCommandResult:
        if value not in {"build", "plan"}:
            return ChatCommandResult(
                True, "usage: /agent build|plan  (or press Tab in the TUI)")
        return ChatCommandResult(True, f"agent: {value}", new_agent=value)

    def _handle_copy(self) -> ChatCommandResult:
        try:
            messages = self.store.messages(self.session_id)
        except (OSError, FileNotFoundError, json.JSONDecodeError):
            return ChatCommandResult(True, "no messages in this session")
        last = next(
            (message for message in reversed(messages)
             if message.role == "assistant" and message.content.strip()),
            None,
        )
        if last is None:
            return ChatCommandResult(True, "no assistant response to copy")
        return ChatCommandResult(True, clipboard=last.content)

    def _handle_trace(self, value: str) -> ChatCommandResult:
        if not value:
            return ChatCommandResult(True, "usage: /trace <decision_id>")
        if self.runtime is None:
            return ChatCommandResult(True, "governed runtime unavailable; pass --vial-root")
        try:
            trace = self.runtime.decision_trace(value)
        except KeyError:
            return ChatCommandResult(True, f"unknown decision: {value}")
        return ChatCommandResult(True, json.dumps(trace, indent=2, ensure_ascii=False))

    def _handle_approve(self, value: str) -> ChatCommandResult:
        if not value:
            return ChatCommandResult(True, "usage: /approve <decision_id>")
        if self.runtime is None:
            return ChatCommandResult(True, "governed runtime unavailable; pass --vial-root")
        try:
            approved = self.runtime.approve_decision(
                value, getattr(self.runtime, "authority", "org-root"),
                note="approved from terminal")
        except (KeyError, ValueError) as error:
            return ChatCommandResult(True, f"error: {error}")
        self.runtime.persist()
        return ChatCommandResult(
            True, f"approved by {approved.approver}: {approved.decision_id}")

    def _handle_decisions(self, value: str) -> ChatCommandResult:
        if self.runtime is None:
            return ChatCommandResult(True, "governed runtime unavailable; pass --vial-root")
        pending = self.runtime.pending_decisions()
        if not pending:
            return ChatCommandResult(True, "no decisions awaiting consensus or approval")
        lines = ["pending decisions:"]
        for row in pending:
            consensus = row.get("consensus")
            consensus_status = (
                f"agreed (ratio={consensus['agreement_ratio']:.2f})"
                if consensus and consensus["agreed"]
                else "disagreed" if consensus
                else "missing")
            lines.append(
                f"  {row['decision_id']}  {row['objective']}"
                f"  risk={row['risk']}"
                f"  consensus={consensus_status}"
                f"  approval={'yes' if row['approval'] else 'no'}")
        return ChatCommandResult(True, "\n".join(lines))

    def _handle_consensus(self, value: str) -> ChatCommandResult:
        if self.runtime is None:
            return ChatCommandResult(True, "governed runtime unavailable; pass --vial-root")
        pending = self.runtime.pending_decisions()
        needing = [
            row for row in pending if row.get("requires_consensus")]
        if not needing:
            return ChatCommandResult(
                True, "no decisions require cross-model consensus")
        lines = ["decisions requiring consensus:"]
        for row in needing:
            consensus = row.get("consensus")
            status = (
                f"agreed (ratio={consensus['agreement_ratio']:.2f})"
                if consensus and consensus["agreed"]
                else "disagreed" if consensus
                else "missing")
            lines.append(
                f"  {row['decision_id']}  {row['objective']}  {status}")
        return ChatCommandResult(True, "\n".join(lines))

    # ------------------------------------------------------------------ #
    # Discovery helpers (used by the TUI picker and tests)
    # ------------------------------------------------------------------ #
    def available_models(self) -> list[str]:
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

    def command_matches(self, buffer: str) -> list[str]:
        text = buffer.strip()
        if not text.startswith("/"):
            return []
        matches = [name for name, _ in COMMANDS if name.startswith(text)]
        return sorted(matches, key=lambda name: (name != text, len(name)))

    def session_previews(self, limit: int = 50) -> list[tuple[str, str]]:
        """``(session_id, preview)`` for the TUI session picker.

        Preview shows the first user message so a session can be recognised
        without opening it. Returns most recent sessions first.
        """
        previews: list[tuple[str, str]] = []
        for session_id in self.store.list():
            try:
                messages = self.store.messages(session_id)
            except (OSError, FileNotFoundError, json.JSONDecodeError):
                continue
            first_user = next(
                (m.content for m in messages if m.role == "user"), "")
            preview = " ".join(first_user.split())[:64] or "(empty)"
            count = len(messages)
            label = f"{preview}  ·  {count} msg"
            previews.append((session_id, label))
            if len(previews) >= limit:
                break
        return previews

    def last_assistant(self) -> str:
        """Plain text of the most recent assistant message (for clipboard)."""
        try:
            messages = self.store.messages(self.session_id)
        except (OSError, FileNotFoundError, json.JSONDecodeError):
            return ""
        last = next(
            (message for message in reversed(messages)
             if message.role == "assistant" and message.content.strip()),
            None,
        )
        return last.content if last is not None else ""


# --------------------------------------------------------------------------- #
# Rendering helpers (also imported by the terminal UI)
# --------------------------------------------------------------------------- #
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


def _friendly_model_error(error: str, model: str) -> str:
    lowered = error.lower()
    if "cannot connect" in lowered or "unable to connect" in lowered:
        return (
            "Cannot connect to the selected model provider.\n"
            f"model: {model}\n"
            "Try one of these:\n"
            "1. /models and then /model provider/model\n"
            "2. vial --providers to verify credentials\n"
            "3. choose a free/local model such as opencode/deepseek-v4-flash-free"
        )
    return f"error: {error}"


HELP_TEXT = f"""VIAL {__version__} — opencode-style terminal UI

Commands:
/models [provider]            list models (registered + opencode)
/model provider/model         switch model (auto = route by prompt)
/model add server/model       add a model to a server
/model remove server/model    remove a model from a server
/agent build|plan             switch agent (or press Tab)
/auto                         toggle auto-approval of workspace permissions
/providers                    list opencode providers
/servers                      list configured servers
/server add <name> <base_url> [api_key_env]
/server remove <name>         remove a server
/server models <name>         list a server's models
/pool                         show the routing pool
/pool add <model_ref>         add a model to the parallel routing pool
/pool remove <model_ref>      remove a model from the pool
/pool set <ref> [<ref> ...]   replace the pool with exactly these models
/events [event_id]            list recent ΔState events
/delta                        capture/compare the materialized project state
/status                       show session status
/trace <decision_id>          show the audit trail of a Decision
/decisions                    list Decisions awaiting consensus or approval
/consensus                    show consensus status of pending Decisions
/approve <decision_id>        approve a pending Decision
/sessions                     list past sessions
/resume <session_id>          resume a past session
/clear                        start a new session
/copy                         copy the last assistant response
/exit                         quit
""".strip()
