from __future__ import annotations

import json
import unittest
import urllib.error

from vial_code_agent.model import HttpModelProvider


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeUrlOpen:
    def __init__(self, payload: dict | Exception) -> None:
        self.payload = payload
        self.requests: list[tuple[str, bytes]] = []

    def __call__(self, request: object, timeout: int | None = None) -> FakeResponse:
        body = getattr(request, "data", b"") or b""
        self.requests.append((request, body))
        if isinstance(self.payload, Exception):
            raise self.payload
        return FakeResponse(json.dumps(self.payload).encode("utf-8"))


class HttpModelProviderTests(unittest.TestCase):
    def test_endpoint_build(self) -> None:
        provider = HttpModelProvider("https://api.example.com/v1", "key", "model")
        self.assertEqual(
            provider._endpoint(), "https://api.example.com/v1/chat/completions"
        )

    def test_chat_parses_response(self) -> None:
        provider = HttpModelProvider("https://api.example.com/v1", "key", "m1")
        payload = {
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }
        with unittest.mock.patch(
            "vial_code_agent.model.urllib.request.urlopen",
            FakeUrlOpen(payload),
        ):
            response = provider.chat("hi")
        self.assertEqual(response.returncode, 0)
        self.assertEqual(response.text, "hello")
        self.assertEqual(response.input_tokens, 5)
        self.assertEqual(response.output_tokens, 2)

    def test_chat_http_error(self) -> None:
        provider = HttpModelProvider("https://api.example.com/v1", "key", "m1")
        with unittest.mock.patch(
            "vial_code_agent.model.urllib.request.urlopen",
            FakeUrlOpen(urllib.error.HTTPError(
                "url", 401, "unauthorized", None, None)),
        ):
            response = provider.chat("hi")
        self.assertEqual(response.returncode, 1)
        self.assertIn("401", response.stderr)

    def test_chat_with_history_builds_messages(self) -> None:
        provider = HttpModelProvider("https://api.example.com/v1", "key", "m1")
        fake = FakeUrlOpen({"choices": [{"message": {"content": "ok"}}], "usage": {}})
        with unittest.mock.patch(
            "vial_code_agent.model.urllib.request.urlopen", fake
        ):
            response = provider.chat(
                "current", history=[("user", "first"), ("assistant", "resp")]
            )
        self.assertEqual(response.returncode, 0)
        body = json.loads(fake.requests[0][1].decode("utf-8"))
        self.assertEqual(
            body["messages"],
            [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "resp"},
                {"role": "user", "content": "current"},
            ],
        )

    def test_list_models(self) -> None:
        provider = HttpModelProvider("https://api.example.com/v1", "key", "m1")
        payload = {"data": [{"id": "alpha"}, {"id": "beta"}]}
        with unittest.mock.patch(
            "vial_code_agent.model.urllib.request.urlopen",
            FakeUrlOpen(payload),
        ):
            listing = provider.list_models()
        self.assertEqual(listing, "alpha\nbeta")


class SessionListTests(unittest.TestCase):
    def test_list_orders_by_recency(self) -> None:
        import tempfile
        from pathlib import Path

        from vial_code_agent.session import SessionStore

        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            first = store.create()
            second = store.create()
            store.append(first, "user", "one")
            store.append(second, "user", "two")
            self.assertEqual(store.list(), [second, first])


if __name__ == "__main__":
    unittest.main()
