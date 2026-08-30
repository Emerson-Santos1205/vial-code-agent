import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

from benchmark.check_provider import _event_summary, check_provider
from vial_code_agent.docker_provider import DockerOpenCodeProvider


class DockerProviderTests(unittest.TestCase):
    @patch("vial_code_agent.docker_provider.subprocess.Popen")
    def test_uses_staged_workspace_and_read_only_credentials(self, popen) -> None:
        process = popen.return_value
        process.returncode = 0
        process.communicate.return_value = (
            '{"type":"text","part":{"text":"diff --git a/a.py b/a.py"}}\n', "")
        provider = DockerOpenCodeProvider("fast")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text("", encoding="utf-8")
            with patch("vial_code_agent.docker_provider.Path.is_file", return_value=True):
                response = provider.generate("fix", root, [root / "a.py"])
        command = popen.call_args.args[0]
        self.assertIn("--mount", command)
        self.assertIn("dst=/workspace", " ".join(command))
        self.assertIn("readonly", " ".join(command))
        self.assertNotIn("fix Return only a unified diff.", command)
        self.assertIn(".vial-opencode-prompt.txt", " ".join(command))
        self.assertEqual(response.returncode, 0)

    def test_provider_health_check_requires_text_and_reports_errors(self) -> None:
        text, errors = _event_summary(json.dumps({
            "type": "error", "error": {"name": "APIError", "message": "denied"}
        }))
        self.assertEqual(text, "")
        self.assertEqual(errors[0]["name"], "APIError")

    @patch("benchmark.check_provider.subprocess.run")
    def test_provider_health_check_accepts_json_text_response(self, run) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = '{"type":"text","part":{"text":"PONG"}}\n'
        run.return_value.stderr = ""
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory) / "auth.json"
            auth.write_text("{}", encoding="utf-8")
            result = check_provider("openai/gpt-5.6-luna", "image", auth)
        self.assertEqual(result["status"], "healthy")
        self.assertTrue(result["response_received"])

    @patch("vial_code_agent.docker_provider.subprocess.Popen")
    def test_docker_provider_preserves_json_error_event(self, popen) -> None:
        process = popen.return_value
        process.returncode = 1
        process.communicate.return_value = (
            '{"type":"error","error":{"message":"model unavailable"}}\n', "")
        provider = DockerOpenCodeProvider("model")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("vial_code_agent.docker_provider.Path.is_file", return_value=True):
                response = provider.generate("fix", root, [])
        self.assertIn("model unavailable", response.stderr)

    @patch("vial_code_agent.docker_provider._terminate_process_tree")
    @patch("vial_code_agent.docker_provider.subprocess.Popen")
    def test_docker_provider_timeout_cleans_process_tree(self, popen, terminate) -> None:
        process = popen.return_value
        process.communicate.side_effect = __import__("subprocess").TimeoutExpired(
            ["docker", "run"], 1)
        process.poll.return_value = None
        provider = DockerOpenCodeProvider("model", timeout_seconds=1)
        with tempfile.TemporaryDirectory() as directory:
            with patch("vial_code_agent.docker_provider.Path.is_file", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    provider.generate("fix", Path(directory), [])
        terminate.assert_called_once_with(process)
