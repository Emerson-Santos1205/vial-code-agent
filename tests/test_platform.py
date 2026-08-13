from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.agents import Agent, MultiAgentTeam
from vial_code_agent.command_runner import CommandRunner
from vial_code_agent.session import SessionStore
from vial_code_agent.tui import (
    TerminalChatUI, _command_palette, _cursor_position, _wrap_input,
)
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

    def test_terminal_chat_commands(self) -> None:
        class Provider:
            def list_models(self, provider: str | None = None) -> str:
                return "openai/test\n"

            def list_providers(self) -> str:
                return "openai\n"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / "sessions")
            session = store.create()
            ui = TerminalChatUI(root, store, session, Provider(), "openai/test", "opencode", False, "plan")
            self.assertIn("session:", ui.handle_command("/status").output)
            self.assertEqual(ui.handle_command("/models").output, "openai/test")
            changed = ui.handle_command("/model openai/other")
            self.assertEqual(changed.new_model, "openai/other")
            cleared = ui.handle_command("/clear")
            self.assertTrue(cleared.new_session_id)
            self.assertTrue(ui.handle_command("/exit").exit)

    def test_terminal_input_wrapping_helpers(self) -> None:
        self.assertEqual(_wrap_input("", 20), [""])
        self.assertEqual(_wrap_input("abcdef", 12), ["abcdef"])
        self.assertEqual(_wrap_input("abc\ndef", 20), ["abc", "def"])
        self.assertEqual(_cursor_position("abcdef", 3, 12), (1, 11))
        self.assertEqual(_cursor_position("abc\ndef", 5, 20), (2, 9))

    def test_command_autocomplete_matches_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / "sessions")
            session = store.create()
            ui = TerminalChatUI(root, store, session, None, "auto", "opencode", False, "plan")
            self.assertEqual(ui._command_matches(list("/ex")), ["/exit"])
            self.assertEqual(ui._command_matches(list("/model a")), ["/model add"])
            self.assertEqual(ui._command_matches(list("/po")), ["/pool", "/pool add", "/pool remove"])
            self.assertEqual(ui._command_matches(list("hello")), [])
            self.assertEqual(ui._command_matches(list("/nope")), [])

    def test_command_palette_renders_selection(self) -> None:
        palette = _command_palette(["/model", "/model add"], 1, 120)
        self.assertEqual(len(palette), 3)
        self.assertIn("Commands", palette[0])
        self.assertIn("/model", palette[1])
        self.assertIn("/model add", palette[2])
        self.assertIn("add a model", palette[2])
