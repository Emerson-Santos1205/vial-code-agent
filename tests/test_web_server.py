from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from vial_code_agent.config import AgentConfig
from vial_code_agent.web_server import make_server


class WebServerTests(unittest.TestCase):
    def _request(self, url: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8") if body else None,
            headers={"Content-Type": "application/json"} if body else {},
            method="POST" if body else "GET")
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_health_and_chat_persist_the_server_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = make_server(Path(directory), AgentConfig(model="test/model"),
                                 "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                self.assertEqual(self._request(base + "/health")["status"], "ok")
                with patch("vial_code_agent.web_server.ChatController.respond",
                           return_value=("answer", "test/model")):
                    first = self._request(base + "/chat", {"message": "hello"})
                    second = self._request(base + "/chat", {
                        "message": "again", "session_id": first["session_id"]})
                self.assertEqual(first["text"], "answer")
                self.assertEqual(second["session_id"], first["session_id"])
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_chat_rejects_missing_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = make_server(Path(directory), AgentConfig(), "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/chat", data=b"{}",
                    headers={"Content-Type": "application/json"}, method="POST")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(error.exception.code, 400)
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
