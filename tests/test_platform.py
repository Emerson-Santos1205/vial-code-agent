from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.agents import Agent, MultiAgentTeam
from vial_code_agent.chat import ChatController
from vial_code_agent.command_runner import CommandRunner
from vial_code_agent.session import SessionStore
from vial_code_agent.workflow import SequentialWorkflow


class PlatformTests(unittest.TestCase):
    def test_session_memory_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            session = store.create()
            store.append(session, "user", "hello")
            self.assertEqual(store.messages(session)[0].content, "hello")

    def test_workflow_stops_after_failure(self) -> None:
        calls: list[str] = []

        def run(task: str, context: str) -> str:
            calls.append(task)
            if task == "fail":
                raise RuntimeError("failed")
            return task

        results = SequentialWorkflow(run).run([("one", "ok"), ("two", "fail"), ("three", "skip")])
        self.assertEqual([result.passed for result in results], [True, False])
        self.assertEqual(calls, ["ok", "fail"])

    def test_multi_agent_fanout(self) -> None:
        team = MultiAgentTeam([
            Agent("reviewer", "review", lambda task: f"review:{task}"),
            Agent("tester", "test", lambda task: f"test:{task}"),
        ])
        self.assertEqual(team.run("change"), {"reviewer": "review:change", "tester": "test:change"})

    def test_command_runner_blocks_unknown_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PermissionError):
                CommandRunner(Path(directory)).run(["format-disk"])

    def test_command_runner_allowlists_python_and_python3(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = CommandRunner(Path(directory))
            self.assertIn("python", runner.allowed)
            self.assertIn("python.exe", runner.allowed)
            self.assertIn("python3", runner.allowed)

    def test_command_runner_rejects_python3_without_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = CommandRunner(
                Path(directory), allowed={"python", "python.exe", "pytest"})
            with self.assertRaises(PermissionError):
                runner.run(["python3", "-c", "print('x')"])

    def _controller(self, directory: str) -> ChatController:
        root = Path(directory)
        store = SessionStore(root / "sessions")
        session = store.create()
        return ChatController(
            root, store, session, _Provider(), "openai/test", "opencode",
            False, "plan",
        )

    def test_chat_controller_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            self.assertIn("session:", controller.handle("/status").output)
            self.assertEqual(controller.handle("/models").output, "openai/test")
            changed = controller.handle("/model openai/other")
            self.assertEqual(changed.new_model, "openai/other")
            agent = controller.handle("/agent plan")
            self.assertEqual(agent.new_agent, "plan")
            toggled = controller.handle("/auto")
            self.assertIn("auto-approve", toggled.output)
            cleared = controller.handle("/clear")
            self.assertTrue(cleared.new_session_id)
            self.assertTrue(controller.handle("/exit").exit)

    def test_chat_controller_autocomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            self.assertEqual(controller.command_matches("/ex"), ["/exit"])
            self.assertEqual(controller.command_matches("/model a"), ["/model add"])
            self.assertEqual(controller.command_matches("/po"), ["/pool", "/pool add", "/pool set", "/pool remove"])
            self.assertEqual(controller.command_matches("hello"), [])
            self.assertEqual(controller.command_matches("/nope"), [])

    def test_chat_controller_governance_commands_need_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            self.assertIn("runtime unavailable", controller.handle("/trace DEC-1").output)
            self.assertIn("runtime unavailable", controller.handle("/approve DEC-1").output)

    def test_copy_command_returns_last_assistant_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            store = controller.store
            store.append(controller.session_id, "user", "hi")
            store.append(controller.session_id, "assistant", "first reply")
            store.append(controller.session_id, "user", "again")
            store.append(controller.session_id, "assistant", "second reply")
            self.assertEqual(controller.last_assistant(), "second reply")
            result = controller.handle("/copy")
            self.assertTrue(result.handled)
            self.assertEqual(result.clipboard, "second reply")
            self.assertEqual(result.output, "")


class _Provider:
    def list_models(self, provider: str | None = None) -> str:
        return "openai/test\n"

    def list_providers(self) -> str:
        return "openai\n"
