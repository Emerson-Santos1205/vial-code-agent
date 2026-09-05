"""Loopback HTTP boundary used by the VS Code extension.

The server deliberately binds to a single workspace selected by the operator;
requests cannot select another filesystem root.
"""
from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .chat import ChatController
from .config import AgentConfig
from .model import OpenCodeProvider
from .servers import ServerRegistry
from .session import SessionStore

OPENAPI_SPEC: dict[str, object] = {
    "openapi": "3.0.3",
    "info": {
        "title": "VIAL Code Agent API",
        "version": "0.3.0",
        "description": "Loopback HTTP boundary for VIAL Code Agent governance and interactive chat.",
    },
    "paths": {
        "/health": {
            "get": {
                "summary": "Health check",
                "responses": {
                    "200": {
                        "description": "Server is healthy",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                },
            }
        },
        "/api/v1/schema": {
            "get": {
                "summary": "OpenAPI 3.0 specification",
                "responses": {
                    "200": {"description": "OpenAPI specification payload"}
                },
            }
        },
        "/chat": {
            "post": {
                "summary": "Send prompt to VIAL Code Agent",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "message": {"type": "string"},
                                    "session_id": {"type": "string"},
                                    "model": {"type": "string"},
                                },
                                "required": ["message"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Chat response and execution route"}
                },
            }
        },
        "/api/v1/decisions": {
            "get": {
                "summary": "List pending decisions awaiting approval or consensus",
                "responses": {
                    "200": {"description": "List of pending decisions"}
                },
            }
        },
        "/api/v1/decisions/{id}/approve": {
            "post": {
                "summary": "Approve a pending decision",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "Decision ID",
                    }
                ],
                "requestBody": {
                    "required": False,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "note": {"type": "string"},
                                    "approver": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Decision approved"},
                    "400": {"description": "Invalid approval request"},
                    "404": {"description": "Decision not found"},
                },
            }
        },
        "/api/v1/trace/{id}": {
            "get": {
                "summary": "Audit trace for a decision",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "Decision ID",
                    }
                ],
                "responses": {
                    "200": {"description": "Audit trace details"},
                    "404": {"description": "Decision not found"},
                },
            }
        },
        "/api/v1/status": {
            "get": {
                "summary": "Runtime snapshot status",
                "responses": {
                    "200": {"description": "Snapshot of organizational runtime"}
                },
            }
        },
    },
}


def make_server(root: Path, config: AgentConfig, host: str, port: int,
                runtime: Any = None) -> ThreadingHTTPServer:
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
            if self.path in ("/api/v1/schema", "/openapi.json"):
                self._send(HTTPStatus.OK, OPENAPI_SPEC)
                return
            if self.path == "/api/v1/decisions":
                if runtime is None:
                    self._send(HTTPStatus.OK, {"decisions": []})
                else:
                    self._send(HTTPStatus.OK, {"decisions": runtime.pending_decisions()})
                return
            if self.path == "/api/v1/status":
                if runtime is None:
                    self._send(HTTPStatus.OK, {"vial_core": "unavailable"})
                else:
                    self._send(HTTPStatus.OK, runtime.snapshot())
                return
            if self.path.startswith("/api/v1/trace/"):
                decision_id = self.path[len("/api/v1/trace/"):].strip()
                if not decision_id:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": "decision_id is required"})
                    return
                if runtime is None:
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "governed runtime unavailable"})
                    return
                trace = runtime.decision_trace(decision_id)
                if not trace.get("found", False):
                    self._send(HTTPStatus.NOT_FOUND, {"error": f"unknown decision '{decision_id}'"})
                    return
                if "decision_id" not in trace:
                    trace["decision_id"] = trace.get("id", decision_id)
                self._send(HTTPStatus.OK, trace)
                return
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.startswith("/api/v1/decisions/") and self.path.endswith("/approve"):
                prefix = "/api/v1/decisions/"
                suffix = "/approve"
                decision_id = self.path[len(prefix):-len(suffix)].strip()
                if not decision_id:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": "decision_id is required"})
                    return
                if runtime is None:
                    self._send(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "governed runtime unavailable"})
                    return
                body: dict[str, Any] = {}
                length = int(self.headers.get("Content-Length", "0"))
                if length > 0:
                    try:
                        body = json.loads(self.rfile.read(length).decode("utf-8"))
                    except json.JSONDecodeError:
                        self._send(HTTPStatus.BAD_REQUEST, {"error": "invalid json payload"})
                        return
                approver = str(body.get("approver") or getattr(runtime, "authority", "org-root"))
                note = str(body.get("note", "approved via API"))
                try:
                    record = runtime.approve_decision(decision_id, approver=approver, note=note)
                    runtime.persist()
                    self._send(HTTPStatus.OK, {
                        "approved": True,
                        "decision_id": decision_id,
                        "approver": record.approver,
                        "note": record.note,
                    })
                except KeyError:
                    self._send(HTTPStatus.NOT_FOUND, {"error": f"unknown decision '{decision_id}'"})
                except (PermissionError, ValueError) as err:
                    self._send(HTTPStatus.BAD_REQUEST, {"error": str(err)})
                return

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
                registry=ServerRegistry(root), runtime=runtime,
                model_timeout=config.model_timeout)
            text, route = controller.respond(message)
            self._send(HTTPStatus.OK, {
                "session_id": session_id, "route": route, "text": text,
            })

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def serve(root: Path, config: AgentConfig, host: str = "127.0.0.1", port: int = 8765,
          runtime: Any = None) -> None:
    server = make_server(root, config, host, port, runtime=runtime)
    print(f"VIAL server listening on http://{host}:{port} for {root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
