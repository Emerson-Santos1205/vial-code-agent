import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from vial_code_agent.docker_provider import DockerOpenCodeProvider


class DockerProviderTests(unittest.TestCase):
    @patch("vial_code_agent.docker_provider.subprocess.run")
    def test_uses_staged_workspace_and_read_only_credentials(self, run) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = '{"type":"text","part":{"text":"diff --git a/a.py b/a.py"}}\n'
        run.return_value.stderr = ""
        provider = DockerOpenCodeProvider("fast")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text("", encoding="utf-8")
            with patch("vial_code_agent.docker_provider.Path.is_file", return_value=True):
                response = provider.generate("fix", root, [root / "a.py"])
        command = run.call_args.args[0]
        self.assertIn("--mount", command)
        self.assertIn("dst=/workspace", " ".join(command))
        self.assertIn("readonly", " ".join(command))
        self.assertNotIn("fix Return only a unified diff.", command)
        self.assertIn(".vial-opencode-prompt.txt", " ".join(command))
        self.assertEqual(response.returncode, 0)
