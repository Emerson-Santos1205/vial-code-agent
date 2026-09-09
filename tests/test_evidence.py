from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.evidence import EvidenceRunner, TestRunnerAdapter, validate_candidate
from vial_code_agent.test_runner import TestResult

PATCH = "--- a/solution.py\n+++ b/solution.py\n@@ -1 +1 @@\n-return 0\n+return 1\n"


class EvidenceTests(unittest.TestCase):
    def test_behavioral_evidence_runs_in_isolated_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "solution.py").write_text("return 0\n", encoding="utf-8")
            result = validate_candidate(
                root, PATCH, ["python", "-c", "from pathlib import Path; assert Path('solution.py').read_text() == 'return 1\\n'"])
            self.assertTrue(result.passed)
            self.assertEqual((root / "solution.py").read_text(), "return 0\n")

    def test_invalid_candidate_has_no_behavioral_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "solution.py").write_text("return 0\n", encoding="utf-8")
            result = validate_candidate(root, "not a patch")
            self.assertFalse(result.passed)


class TestRunnerAdapterTests(unittest.TestCase):
    def test_run_tests_returns_test_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = TestRunnerAdapter()
            result = adapter.run_tests(root, ["python", "-c", "print('hello')"], 30)
            self.assertIsInstance(result, TestResult)
            self.assertEqual(result.returncode, 0)
            self.assertIn("hello", result.stdout)
            self.assertTrue(result.passed)

    def test_run_tests_rejects_non_allowlisted_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = TestRunnerAdapter()
            result = adapter.run_tests(root, ["rm", "-rf", "/"], 10)
            self.assertEqual(result.returncode, 126)
            self.assertFalse(result.passed)
            self.assertIn("not allowlisted", result.stderr)

    def test_run_tests_timeout_returns_124(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = TestRunnerAdapter()
            result = adapter.run_tests(
                root, ["python", "-c", "import time; time.sleep(10)"], 1)
            self.assertEqual(result.returncode, 124)
            self.assertFalse(result.passed)

    def test_run_tests_failing_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = TestRunnerAdapter()
            result = adapter.run_tests(
                root, ["python", "-c", "import sys; sys.exit(1)"], 10)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(result.passed)

    def test_adapter_with_custom_runner(self) -> None:
        runner = EvidenceRunner(network_enabled=True, timeout=5)
        adapter = TestRunnerAdapter(runner)
        self.assertIs(adapter.runner, runner)
        self.assertTrue(runner.network_enabled)

    def test_adapter_default_runner(self) -> None:
        adapter = TestRunnerAdapter()
        self.assertIsInstance(adapter.runner, EvidenceRunner)
        self.assertFalse(adapter.runner.network_enabled)
