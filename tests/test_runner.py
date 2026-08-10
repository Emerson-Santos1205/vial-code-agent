from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from vial_code_agent.test_runner import run_tests


class TestRunnerTests(unittest.TestCase):
    def test_runner_executes_in_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_tests(Path(directory), [sys.executable, "-c", "print('ok')"])

            self.assertTrue(result.passed)
            self.assertEqual(result.returncode, 0)
            self.assertIn("ok", result.stdout)
