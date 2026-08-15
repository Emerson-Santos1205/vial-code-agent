from __future__ import annotations

import subprocess
import unittest

from vial_code_agent.model import OpenCodeProvider, _extract_error


class ExtractErrorTests(unittest.TestCase):
    def _process(self, stdout: str, stderr: str = "", returncode: int = 1) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(["opencode", "run"], returncode, stdout, stderr)

    def test_extracts_message_nested_under_data(self) -> None:
        process = self._process(
            '{"type":"error","error":{"name":"UnknownError","data":{"message":"Unexpected server error.","ref":"err_123"}}}'
        )
        self.assertEqual(
            _extract_error(process),
            "Unexpected server error. (ref err_123)",
        )

    def test_prefers_stderr_when_present(self) -> None:
        process = self._process('{"type":"error","error":{"message":"stdout error"}}', stderr="real stderr")
        self.assertEqual(_extract_error(process), "real stderr")

    def test_empty_when_no_error_event(self) -> None:
        process = self._process(
            '{"type":"text","part":{"text":"ok"}}\n{"type":"step_finish","part":{"tokens":{"total":5}}}',
            returncode=0,
        )
        self.assertEqual(_extract_error(process), "")


class OpenCodeProviderTests(unittest.TestCase):
    def test_model_alias_resolves(self) -> None:
        self.assertEqual(OpenCodeProvider("fast").model, "openai/gpt-5.6-luna-fast")

    def test_with_history_truncates_oversized_context(self) -> None:
        from vial_code_agent.model import _MAX_CONTEXT_CHARS, _with_history
        big = "x" * (_MAX_CONTEXT_CHARS + 500)
        prompt = _with_history("ask", [("user", big)])
        self.assertLessEqual(len(prompt), _MAX_CONTEXT_CHARS + 64)
        self.assertTrue(prompt.endswith("user: ask"))

    def test_chat_injects_history_into_prompt(self) -> None:
        provider = OpenCodeProvider("fast", executable="opencode")
        captured: dict[str, object] = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            return subprocess.CompletedProcess(command, 0, "", "")

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.run", side_effect=fake_run
        ):
            provider.chat(
                "translate to Portuguese",
                history=[("user", "write a greeting"), ("assistant", "Olá")],
            )
        prompt = captured["command"][-1]
        self.assertIn("write a greeting", prompt)
        self.assertIn("Olá", prompt)
        self.assertTrue(prompt.endswith("user: translate to Portuguese"))


if __name__ == "__main__":
    unittest.main()
