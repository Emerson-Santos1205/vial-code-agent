from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.agents import Agent, MultiAgentTeam
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
