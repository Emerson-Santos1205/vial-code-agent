from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.chat import ChatController
from vial_code_agent.model import ModelResponse, OpenCodeProvider
from vial_code_agent.session import SessionStore


class _FakeDecision:
    model = "openai/gpt-5.6-luna-fast"
    tier = "advanced"


class _FakeRouting:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def dispatch(self, task, root=None, requested_model="auto", history=None):
        self.calls.append((task, history))
        return ModelResponse("answer", 0), _FakeDecision()


class ChatMemoryTests(unittest.TestCase):
    def _controller(self, directory: str) -> tuple[ChatController, _FakeRouting]:
        root = Path(directory)
        store = SessionStore(root / "sessions")
        controller = ChatController(
            root, store, store.create(),
            OpenCodeProvider("openai/gpt-5.6-luna-fast"),
            "auto", "opencode", False, "plan",
        )
        routing = _FakeRouting()
        controller.routing = routing
        return controller, routing

    def test_first_prompt_has_no_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, routing = self._controller(directory)
            controller.respond("first question")
            self.assertEqual(routing.calls[0][1], [])

    def test_second_prompt_receives_prior_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, routing = self._controller(directory)
            controller.respond("create a function")
            controller.respond("translate the docstring")
            history = routing.calls[1][1]
            self.assertEqual(
                history,
                [("user", "create a function"), ("assistant", "answer")],
            )

    def test_resume_loads_session_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, routing = self._controller(directory)
            controller.respond("remember this phrase")
            resumed = ChatController(
                Path(directory), controller.store, controller.session_id,
                OpenCodeProvider("openai/gpt-5.6-luna-fast"),
                "auto", "opencode", False, "plan",
            )
            resumed.routing = routing
            resumed.respond("what did I ask?")
            self.assertEqual(
                routing.calls[-1][1][0], ("user", "remember this phrase"))


if __name__ == "__main__":
    unittest.main()
