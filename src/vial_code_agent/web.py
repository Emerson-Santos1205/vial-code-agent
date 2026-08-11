from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .session import SessionStore
from .model import OpenCodeProvider


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765, provider: OpenCodeProvider | None = None) -> None:
    store = SessionStore(root / ".vial-sessions")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if urlparse(self.path).path == "/health":
                self._json({"status": "ok"})
                return
            self._html("<h1>VIAL Code Agent</h1><p>POST /chat with session_id and message.</p>")

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/chat":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or b"{}")
            session_id = data.get("session_id") or store.create()
            message = str(data.get("message", ""))
            if not message:
                self.send_error(400, "message is required")
                return
            store.append(session_id, "user", message)
            if provider is not None:
                response = provider.chat(message, root)
                store.append(session_id, "assistant", response.text)
            self._json({"session_id": session_id, "messages": [m.__dict__ for m in store.messages(session_id)]})

        def _json(self, value: object) -> None:
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, body: str) -> None:
            data = f"<!doctype html><html><body>{body}</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            return

    ThreadingHTTPServer((host, port), Handler).serve_forever()
