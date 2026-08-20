import unittest

from benchmark.run_benchmark import summarize
from benchmark.run_swebench import _failure_class


class BenchmarkMetricTests(unittest.TestCase):
    def test_summary_reports_patch_and_test_failures(self) -> None:
        rows = [
            {
                "passed": False, "regression": True,
                "human_intervention": True, "rollback": True,
                "patch_failure": True, "failure_stage": "patch_contract",
                "attempts": 3, "elapsed_seconds": 1.0,
                "input_tokens": 10, "output_tokens": 5,
            },
            {
                "passed": True, "regression": False,
                "human_intervention": False, "rollback": False,
                "patch_failure": False, "failure_stage": "",
                "attempts": 1, "elapsed_seconds": 2.0,
                "input_tokens": 20, "output_tokens": 10,
            },
        ]

        report = summarize(rows)

        self.assertEqual(report["patch_failures"], 1)
        self.assertEqual(report["patch_failure_rate"], 0.5)
        self.assertEqual(report["test_failures"], 0)

    def test_swebench_failure_classes_keep_environment_separate(self) -> None:
        self.assertEqual(_failure_class("patch_validation"), "patch")
        self.assertEqual(_failure_class("test_environment"), "environment")
        self.assertEqual(
            _failure_class("tests", "ImportError while loading conftest"),
            "environment",
        )
        self.assertEqual(_failure_class("tests", "assertion failed"), "tests")
