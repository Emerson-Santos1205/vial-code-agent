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

    def test_openapi_schema_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = make_server(Path(directory), AgentConfig(), "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                schema = self._request(base + "/api/v1/schema")
                self.assertEqual(schema["openapi"], "3.0.3")
                self.assertEqual(schema["info"]["title"], "VIAL Code Agent API")
                self.assertIn("/api/v1/decisions", schema["paths"])
                self.assertIn("/api/v1/decisions/{id}/approve", schema["paths"])
                self.assertIn("/api/v1/trace/{id}", schema["paths"])
                self.assertIn("/api/v1/status", schema["paths"])
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

    def test_governance_endpoints_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = make_server(Path(directory), AgentConfig(), "127.0.0.1", 0, runtime=None)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                decisions = self._request(base + "/api/v1/decisions")
                self.assertEqual(decisions, {"decisions": []})
                status = self._request(base + "/api/v1/status")
                self.assertEqual(status, {"vial_core": "unavailable"})

                req_trace = urllib.request.Request(base + "/api/v1/trace/DEC-1")
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(req_trace, timeout=2)
                self.assertEqual(err.exception.code, 503)

                req_approve = urllib.request.Request(
                    base + "/api/v1/decisions/DEC-1/approve", data=b"{}",
                    headers={"Content-Type": "application/json"}, method="POST")
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(req_approve, timeout=2)
                self.assertEqual(err.exception.code, 503)
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_governance_endpoints_with_runtime(self) -> None:
        from vial_code_agent.core import VialCoreReference
        from vial_code_agent.vial_runtime import VialRuntime

        root = Path(__file__).resolve().parents[1]
        reference = VialCoreReference(root / "vendor" / "vial-core")
        if not reference.exists():
            self.skipTest("VIAL submodule is not initialized")

        with tempfile.TemporaryDirectory() as directory:
            dir_path = Path(directory)
            runtime = VialRuntime(reference, dir_path / "vial-state")
            decision = runtime.propose_decision("governed api operation", risk="medium")

            server = make_server(dir_path, AgentConfig(), "127.0.0.1", 0, runtime=runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                status = self._request(base + "/api/v1/status")
                self.assertEqual(status["organization_id"], "ORG-VIAL-CODE-AGENT")

                decisions = self._request(base + "/api/v1/decisions")
                decision_ids = [d["decision_id"] for d in decisions["decisions"]]
                self.assertIn(decision.id, decision_ids)

                approve_resp = self._request(
                    f"{base}/api/v1/decisions/{decision.id}/approve",
                    {"note": "approved via test"})
                self.assertTrue(approve_resp["approved"])
                self.assertEqual(approve_resp["decision_id"], decision.id)

                trace = self._request(f"{base}/api/v1/trace/{decision.id}")
                self.assertEqual(trace["decision_id"], decision.id)

                # Unknown decision returns 404
                req_trace_404 = urllib.request.Request(f"{base}/api/v1/trace/NONEXISTENT")
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(req_trace_404, timeout=2)
                self.assertEqual(err.exception.code, 404)

                req_approve_404 = urllib.request.Request(
                    f"{base}/api/v1/decisions/NONEXISTENT/approve", data=b"{}",
                    headers={"Content-Type": "application/json"}, method="POST")
                with self.assertRaises(urllib.error.HTTPError) as err:
                    urllib.request.urlopen(req_approve_404, timeout=2)
                self.assertEqual(err.exception.code, 404)
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()
