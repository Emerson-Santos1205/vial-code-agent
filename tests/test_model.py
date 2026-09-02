from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from vial_code_agent.model import (
    HttpModelProvider,
    OpenCodeProvider,
    _extract_error,
    _find_diff_text,
    _is_text_event,
    _parse_events,
    _trim_messages,
    _with_history,
    extract_diff,
)


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


class ExtractDiffTests(unittest.TestCase):
    def test_plain_diff(self) -> None:
        diff = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
        self.assertEqual(extract_diff(diff), diff)

    def test_diff_git_prefix(self) -> None:
        diff = ("preamble diff --git a/x b/x\n"
                "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n")
        result = extract_diff(diff)
        self.assertTrue(result.startswith("diff --git "))
        self.assertIn("--- a/x", result)

    def test_fenced_diff(self) -> None:
        fenced = 'text\n```diff\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n```\ntail'
        self.assertEqual(
            extract_diff(fenced),
            "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n",
        )

    def test_prose_with_embedded_diff(self) -> None:
        prose = 'Here is the change:\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new'
        self.assertEqual(extract_diff(prose).startswith("--- a/x"), True)

    def test_apply_patch_end_marker_is_removed(self) -> None:
        response = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n*** End Patch"
        self.assertEqual(
            extract_diff(response),
            "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n",
        )

    def test_no_diff_returns_none(self) -> None:
        self.assertIsNone(extract_diff("nothing here"))

    def test_find_diff_text_nested(self) -> None:
        nested = {"result": {"lines": ["--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"]}}
        self.assertEqual(
            _find_diff_text(nested),
            "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n",
        )

    def test_find_diff_text_none(self) -> None:
        self.assertIsNone(_find_diff_text({"no": "diff"}))

    def test_is_text_event(self) -> None:
        self.assertTrue(_is_text_event('{"type":"text","part":{"text":"hi"}}'))
        self.assertFalse(_is_text_event("not json"))
        self.assertFalse(_is_text_event('{"type":"step_finish"}'))

    def test_parse_events_ignores_malformed_optional_parts(self) -> None:
        text, usage = _parse_events(
            '{"type":"text","part":null}\n'
            '{"type":"text","part":{"text":"ok"}}\n'
            '{"type":"step_finish","part":null}'
        )
        self.assertEqual(text, "ok")
        self.assertEqual(usage, {})

class OpenCodeProviderTests(unittest.TestCase):
    def test_model_alias_resolves(self) -> None:
        self.assertEqual(OpenCodeProvider("fast").model, "opencode/big-pickle")

    def test_with_history_truncates_oversized_context(self) -> None:
        from vial_code_agent.model import _MAX_CONTEXT_CHARS
        big = "x" * (_MAX_CONTEXT_CHARS + 500)
        prompt = _with_history("ask", [("user", big)])
        self.assertLessEqual(len(prompt), _MAX_CONTEXT_CHARS + 64)
        self.assertTrue(prompt.endswith("user: ask"))

    def test_chat_injects_history_into_prompt(self) -> None:
        provider = OpenCodeProvider("fast", executable="opencode")
        captured: dict[str, object] = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["input"] = kwargs.get("input")
            return subprocess.CompletedProcess(command, 0, "", "")

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.run", side_effect=fake_run
        ):
            provider.chat(
                "translate to Portuguese",
                history=[("user", "write a greeting"), ("assistant", "Olá")],
            )
        prompt = captured["input"] or captured["command"][-1]
        self.assertIn("write a greeting", prompt)
        self.assertIn("Olá", prompt)
        self.assertTrue(prompt.endswith("user: translate to Portuguese"))

    def test_chat_parses_text_events(self) -> None:
        provider = OpenCodeProvider("fast", executable="opencode")
        events = (
            '{"type":"text","part":{"text":"Hello "}}\n'
            '{"type":"text","part":{"text":"world"}}\n'
            '{"type":"step_finish","part":{}}\n'
        )

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, events, "")

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.run", side_effect=fake_run
        ):
            response = provider.chat("hi")
        self.assertEqual(response.text, "Hello world")

    def test_chat_with_auto_approve(self) -> None:
        provider = OpenCodeProvider("fast", executable="opencode", auto_approve=True)
        captured: dict[str, object] = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            return subprocess.CompletedProcess(command, 0, "", "")

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.run", side_effect=fake_run
        ):
            provider.chat("hi")
        self.assertEqual(captured["command"][2], "--auto")

    def test_chat_stream_yields_chunks_and_records_last_response(self) -> None:
        provider = OpenCodeProvider("fast", executable="opencode")
        events = (
            '{"type":"text","part":{"text":"stream "}}\n'
            '{"type":"text","part":{"text":"reply"}}\n'
            '{"type":"step_finish","part":{}}\n'
        )

        class _FakeStream:
            def read(self) -> str:
                return ""

            def __iter__(self):
                return iter(events.splitlines())

        class _FakePopen:
            def __init__(self, command, **kwargs) -> None:
                self.command = command
                self.stdout = _FakeStream()
                self.stderr = _FakeStream()
                self.stdin = None
                self.returncode = 0
                self._lines = iter(events.splitlines())

            def __iter__(self) -> _FakePopen:
                return self

            def __next__(self) -> str:
                return next(self._lines)

            def wait(self, timeout: int | None = None) -> int:
                return 0

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.Popen", side_effect=_FakePopen
        ):
            chunks = list(provider.chat_stream("hi"))
        self.assertEqual("".join(chunks), "stream reply")
        self.assertIsNotNone(provider.last_response)
        self.assertEqual(provider.last_response.text, "stream reply")
        self.assertEqual(provider.last_response.returncode, 0)

    def test_chat_stream_injects_history(self) -> None:
        provider = OpenCodeProvider("fast", executable="opencode")
        captured: dict[str, object] = {}

        class _FakeStream:
            def read(self) -> str:
                return ""

            def __iter__(self):
                return iter(())

        class _FakePopen:
            def __init__(self, command, **kwargs) -> None:
                captured["command"] = command
                captured["input"] = ""
                self.stdout = _FakeStream()
                self.stderr = _FakeStream()
                self.stdin = self
                self.returncode = 0

            def write(self, value: str) -> None:
                captured["input"] = value

            def close(self) -> None:
                pass

            def wait(self, timeout: int | None = None) -> int:
                return 0

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.Popen", side_effect=_FakePopen
        ):
            list(provider.chat_stream("follow up", history=[("user", "first")]))
        prompt = captured["input"] or captured["command"][-1]
        self.assertIn("first", prompt)
        self.assertTrue(prompt.endswith("user: follow up"))

    def test_cancel_terminates_active_process(self) -> None:
        provider = OpenCodeProvider("fast", executable="opencode")
        terminated = []

        class _FakeProc:
            def poll(self):
                return None

            def terminate(self) -> None:
                terminated.append(True)

        provider._active_proc = _FakeProc()
        provider.cancel()
        self.assertEqual(terminated, [True])
        provider._active_proc = None
        provider.cancel()  # no-op without an active process

    def test_generate_builds_command_and_parses(self) -> None:
        provider = OpenCodeProvider(
            "fast", executable="opencode", auto_approve=True, agent="build")
        events = (
            '{"type":"text","part":{"text":"--- a/x\\n+++ b/x\\n"}}\n'
            '{"type":"step_finish","part":{"tokens":{"input":10,"output":5,"total":15}}}\n'
        )
        captured: dict[str, object] = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            return subprocess.CompletedProcess(command, 0, events, "")

        with tempfile.TemporaryDirectory() as directory:
            with unittest.mock.patch(
                "vial_code_agent.model.subprocess.run", side_effect=fake_run
            ):
                response = provider.generate(
                    "add feature", directory=Path(directory), files=[Path("a.py")])
        self.assertEqual(response.returncode, 0)
        self.assertIn("--auto", captured["command"])
        self.assertIn("--agent", captured["command"])
        self.assertIn("--file=a.py", captured["command"])
        self.assertEqual(response.input_tokens, 10)
        self.assertEqual(response.output_tokens, 5)
        self.assertEqual(response.total_tokens, 15)

    def test_generate_falls_back_to_nested_text(self) -> None:
        provider = OpenCodeProvider("fast", executable="opencode")
        events = json.dumps({"result": {"text": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"}})

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, events, "")

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.run", side_effect=fake_run
        ):
            response = provider.generate("change")
        self.assertIn("--- a/x", response.text)

    def test_generate_executable_not_found(self) -> None:
        provider = OpenCodeProvider("fast", executable="definitely-missing-vial-cmd")

        def raise_missing(command, **kwargs):
            raise FileNotFoundError("no such file")

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.run", side_effect=raise_missing
        ):
            with self.assertRaisesRegex(RuntimeError, "model executable not found"):
                provider.generate("change")

    def test_generate_timeout(self) -> None:
        provider = OpenCodeProvider("fast", executable="opencode")

        def raise_timeout(command, **kwargs):
            raise subprocess.TimeoutExpired(command, timeout=10)

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.run", side_effect=raise_timeout
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                provider.generate("change")

    def test_generate_winerror_206(self) -> None:
        provider = OpenCodeProvider("fast", executable="opencode")

        def raise_206(command, **kwargs):
            error = FileNotFoundError("cmd is too long")
            error.winerror = 206
            raise error

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.run", side_effect=raise_206
        ):
            with self.assertRaisesRegex(RuntimeError, "too large for Windows"):
                provider.generate("change")

    def test_list_models_success_and_error(self) -> None:
        provider = OpenCodeProvider("fast", executable="opencode")

        def ok(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "openai/x\n", "")

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.run", side_effect=ok
        ):
            listing = provider.list_models("openai")
        self.assertEqual(listing, "openai/x\n")

        def failing(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "boom")

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.run", side_effect=failing
        ):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                provider.list_models()

    def test_list_providers_success_and_error(self) -> None:
        provider = OpenCodeProvider("fast", executable="opencode")

        def ok(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "openai\n", "")

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.run", side_effect=ok
        ):
            listing = provider.list_providers()
        self.assertEqual(listing, "openai\n")

        def failing(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "down")

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.run", side_effect=failing
        ):
            with self.assertRaisesRegex(RuntimeError, "down"):
                provider.list_providers()

    def test_trim_messages_drops_oldest(self) -> None:
        from vial_code_agent.model import _MAX_CONTEXT_CHARS
        messages = [
            {"role": "user", "content": "a" * (_MAX_CONTEXT_CHARS // 2)},
            {"role": "assistant", "content": "b" * (_MAX_CONTEXT_CHARS // 2)},
            {"role": "user", "content": "c"},
        ]
        _trim_messages(messages)
        self.assertEqual(messages[-1]["content"], "c")
        self.assertLessEqual(
            sum(len(m["content"]) for m in messages), _MAX_CONTEXT_CHARS)

    def test_resolve_executable_via_which(self) -> None:
        from vial_code_agent.model import _resolve_executable
        resolved = _resolve_executable("python")
        self.assertTrue(os.path.basename(resolved).startswith("python"))

    def test_with_history_truncation_drops_partial_line(self) -> None:
        from vial_code_agent.model import _MAX_CONTEXT_CHARS
        big = "line\n" + "x" * (_MAX_CONTEXT_CHARS + 500)
        prompt = _with_history("ask", [("user", big)])
        self.assertTrue(prompt.startswith("user: ask") or "user: ask" in prompt)
        self.assertFalse(prompt.split("\n")[0] == "")

    def test_with_history_truncation_splits_at_newline(self) -> None:
        from vial_code_agent.model import _MAX_CONTEXT_CHARS
        prefix = "a" * (_MAX_CONTEXT_CHARS - 10)
        context = f"{prefix}\nline-one\nline-two"
        prompt = _with_history("ask", [("user", context)])
        self.assertIn("line-one", prompt)
        self.assertIn("line-two", prompt)
        self.assertNotIn("a" * 50, prompt)

    def test_generate_ignores_non_json_lines(self) -> None:
        provider = OpenCodeProvider("fast", executable="opencode")
        events = (
            "not json at all\n"
            '{"type":"text","part":{"text":"--- a/x\\n+++ b/x\\n@@ -1 +1 @@\\n-old\\n+new\\n"}}\n'
        )

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, events, "")

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.run", side_effect=fake_run
        ):
            response = provider.generate("change")
        self.assertIn("--- a/x", response.text)

    def test_generate_fallback_skips_invalid_lines(self) -> None:
        provider = OpenCodeProvider("fast", executable="opencode")
        events = (
            '{"type":"step_finish","part":{}}\n'
            "not json\n"
            '{"result":{"text":"--- a/x\\n+++ b/x\\n@@ -1 +1 @@\\n-old\\n+new\\n"}}\n'
        )

        def fake_run(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, events, "")

        with unittest.mock.patch(
            "vial_code_agent.model.subprocess.run", side_effect=fake_run
        ):
            response = provider.generate("change")
        self.assertIn("--- a/x", response.text)

    def test_extract_diff_header_only(self) -> None:
        diff = "random text --- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
        self.assertEqual(extract_diff(diff).startswith("--- a/x"), True)

    def test_extract_diff_after_newline(self) -> None:
        diff = "intro\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
        self.assertEqual(extract_diff(diff).startswith("--- a/x"), True)

    def test_extract_error_skips_invalid_lines(self) -> None:
        process = subprocess.CompletedProcess(
            ["opencode", "run"], 1,
            'garbage\n{"type":"error","error":"raw error"}\n', "")
        self.assertEqual(_extract_error(process), "raw error")

    def test_extract_error_scalar_error(self) -> None:
        process = subprocess.CompletedProcess(
            ["opencode", "run"], 1,
            '{"type":"error","error":{"message":"plain"}}\n'
            '{"type":"error","error":"fallback"}\n', "")
        self.assertEqual(_extract_error(process), "plain")

    def test_resolve_executable_npm_fallback(self) -> None:
        from vial_code_agent.model import _resolve_executable
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            npm_dir = home / "AppData" / "Roaming" / "npm"
            npm_dir.mkdir(parents=True)
            (npm_dir / "opencode.cmd").write_text("@echo off\n", encoding="utf-8")
            with patch("vial_code_agent.model.shutil.which", return_value=None):
                with patch("vial_code_agent.model.Path.home", return_value=home):
                    resolved = _resolve_executable("opencode")
            self.assertEqual(resolved, str(npm_dir / "opencode.cmd"))

    def test_resolve_executable_returns_original(self) -> None:
        from vial_code_agent.model import _resolve_executable
        with patch("vial_code_agent.model.shutil.which", return_value=None):
            resolved = _resolve_executable("missing-cmd")
        self.assertEqual(resolved, "missing-cmd")


class HttpModelProviderEdgeTests(unittest.TestCase):
    def test_endpoint_variants(self) -> None:
        full = HttpModelProvider("https://api.example.com/v1/chat/completions", "", "m")
        self.assertEqual(full._endpoint(), "https://api.example.com/v1/chat/completions")
        plain = HttpModelProvider("https://api.example.com", "", "m")
        self.assertEqual(plain._endpoint(), "https://api.example.com/v1/chat/completions")

    def test_chat_connection_error(self) -> None:
        provider = HttpModelProvider("https://api.example.com/v1", "key", "m1")
        with unittest.mock.patch(
            "vial_code_agent.model.urllib.request.urlopen",
            side_effect=urllib.error.URLError("no route"),
        ):
            response = provider.chat("hi")
        self.assertEqual(response.returncode, 1)
        self.assertIn("cannot connect", response.stderr)

    def test_chat_invalid_response(self) -> None:
        provider = HttpModelProvider("https://api.example.com/v1", "key", "m1")
        with unittest.mock.patch(
            "vial_code_agent.model.urllib.request.urlopen",
            side_effect=lambda request, timeout=None: _JsonBody("[]"),
        ):
            response = provider.chat("hi")
        self.assertEqual(response.returncode, 1)
        self.assertIn("invalid response", response.stderr)

    def test_list_models_error(self) -> None:
        provider = HttpModelProvider("https://api.example.com/v1", "key", "m1")
        with unittest.mock.patch(
            "vial_code_agent.model.urllib.request.urlopen",
            side_effect=urllib.error.URLError("no route"),
        ):
            with self.assertRaisesRegex(RuntimeError, "cannot connect"):
                provider.list_models()

    def test_list_models_http_error(self) -> None:
        provider = HttpModelProvider("https://api.example.com/v1", "key", "m1")
        with unittest.mock.patch(
            "vial_code_agent.model.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError(
                "url", 500, "server error", None, None),
        ):
            with self.assertRaisesRegex(RuntimeError, "500"):
                provider.list_models()

    def test_as_int_rejects_bad_values(self) -> None:
        from vial_code_agent.model import _as_int
        self.assertIsNone(_as_int("not-a-number"))
        self.assertIsNone(_as_int(None))


class _JsonBody:
    def __init__(self, body: object) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self) -> _JsonBody:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


if __name__ == "__main__":
    unittest.main()
