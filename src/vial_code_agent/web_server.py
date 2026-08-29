"""Loopback HTTP boundary used by the VS Code extension.

The server deliberately binds to a single workspace selected by the operator;
requests cannot select another filesystem root.
"""
from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .chat import ChatController
from .config import AgentConfig
from .model import OpenCodeProvider
from .servers import ServerRegistry
from .session import SessionStore


def make_server(root: Path, config: AgentConfig, host: str, port: int) -> ThreadingHTTPServer:
    root = root.resolve()
    store = SessionStore(root / ".vial-sessions")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict[str, object]) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                self._send(HTTPStatus.OK, {"status": "ok", "root": str(root)})
                return
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/chat":
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                message = str(body["message"]).strip()
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self._send(HTTPStatus.BAD_REQUEST, {"error": "message is required"})
                return
            if not message:
                self._send(HTTPStatus.BAD_REQUEST, {"error": "message is required"})
                return
            session_id = str(body.get("session_id", ""))
            try:
                if session_id:
                    store.messages(session_id)
                else:
                    session_id = store.create()
            except (OSError, FileNotFoundError, json.JSONDecodeError):
                self._send(HTTPStatus.BAD_REQUEST, {"error": "unknown session_id"})
                return
            model = str(body.get("model") or config.model)
            provider = OpenCodeProvider(
                model, config.opencode_executable, config.auto_approve,
                config.opencode_agent, config.model_timeout)
            controller = ChatController(
                root, store, session_id, provider, model, config.opencode_executable,
                config.auto_approve, config.opencode_agent,
                registry=ServerRegistry(root), model_timeout=config.model_timeout)
            text, route = controller.respond(message)
            self._send(HTTPStatus.OK, {
                "session_id": session_id, "route": route, "text": text,
            })

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve(root: Path, config: AgentConfig, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = make_server(root, config, host, port)
    print(f"VIAL server listening on http://{host}:{port} for {root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
