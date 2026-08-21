import unittest
import subprocess
import tempfile
from pathlib import Path

from benchmark.run_benchmark import classify_failure, summarize
from benchmark.run_swebench import (
    _failure_class, _failure_subclass, build_swebench_prompt, select_test_image,
    _candidate_consensus, _run_test_groups, baseline_is_valid,
    should_retry_test_failure,
    success_metrics,
)
from benchmark.instance import InstanceSpec
from benchmark.report import success_metrics as report_success_metrics
from benchmark.swebench_environment import EnvironmentResolver


class BenchmarkMetricTests(unittest.TestCase):
    def test_summary_reports_patch_and_test_failures(self) -> None:
        rows = [
            {
                "passed": False, "regression": True,
                "human_intervention": True, "rollback": True,
                "patch_failure": True, "failure_stage": "patch_contract",
                "failure_class": "patch", "failure_subclass": "malformed",
                "attempts": 3, "elapsed_seconds": 1.0,
                "input_tokens": 10, "output_tokens": 5,
            },
            {
                "passed": True, "regression": False,
                "human_intervention": False, "rollback": False,
                "patch_failure": False, "failure_stage": "",
                "failure_class": "none", "failure_subclass": "none",
                "attempts": 1, "elapsed_seconds": 2.0,
                "input_tokens": 20, "output_tokens": 10,
            },
        ]

        report = summarize(rows)

        self.assertEqual(report["patch_failures"], 1)
        self.assertEqual(report["patch_failure_rate"], 0.5)
        self.assertEqual(report["test_failures"], 0)
        self.assertEqual(report["failure_breakdown"]["patch.malformed"], 1)

    def test_success_metrics_separate_environment_failures(self) -> None:
        results = [
            {"passed": True, "failure_class": "none"},
            {"passed": False, "failure_class": "tests"},
            {"passed": False, "failure_class": "environment"},
        ]

        metrics = success_metrics(results)

        self.assertEqual(metrics["environment_valid"], 2)
        self.assertEqual(metrics["agent_solved"], 1)
        self.assertEqual(metrics["environment_valid_rate"], 2 / 3)
        self.assertEqual(metrics["agent_success_rate"], 0.5)
        self.assertAlmostEqual(metrics["end_to_end_success_rate"], 1 / 3)
        self.assertEqual(report_success_metrics(results), metrics)

    def test_instance_contract_is_metadata_only(self) -> None:
        instance = InstanceSpec.from_dict({
            "id": "task-1", "repo": "org/repo", "base_commit": "abc",
            "fail_to_pass": ["tests/test.py::test_fix"],
        })
        self.assertEqual(instance.base_commit, "abc")
        self.assertEqual(instance.fail_to_pass, ("tests/test.py::test_fix",))

    def test_local_failure_classes_are_specific(self) -> None:
        self.assertEqual(
            classify_failure("patch_contract", "patch is a no-op"),
            ("patch", "no_op"),
        )
        self.assertEqual(
            classify_failure("patch", "patch path escapes workspace"),
            ("patch", "path_violation"),
        )
        self.assertEqual(
            classify_failure("environment", "model request timed out"),
            ("environment", "infrastructure"),
        )

    def test_swebench_failure_classes_keep_environment_separate(self) -> None:
        self.assertEqual(_failure_class("patch_validation"), "patch")
        self.assertEqual(_failure_class("test_environment"), "environment")
        self.assertEqual(
            _failure_class("tests", "ImportError while loading conftest"),
            "environment",
        )
        self.assertEqual(_failure_class("tests", "assertion failed"), "tests")
        self.assertEqual(
            _failure_subclass("patch_contract", "patch is a no-op"), "no_op")
        self.assertEqual(
            _failure_subclass(
                "tests", "assertion failed",
                {"passed": False, "fail_to_pass": True, "pass_to_pass": False}),
            "regression")

    def test_swebench_runtime_is_selected_per_repository(self) -> None:
        astropy_image, astropy_python = select_test_image({"repo": "astropy/astropy"})
        django_image, django_python = select_test_image({"repo": "django/django"})
        self.assertIn("python39", astropy_image)
        self.assertEqual(astropy_python, "3.9")
        self.assertIn("python38", django_image)
        self.assertEqual(django_python, "3.8")
        override, _ = select_test_image({"repo": "astropy/astropy"}, "custom:tag")
        self.assertEqual(override, "custom:tag")

    def test_environment_spec_preserves_instance_contract(self) -> None:
        spec = EnvironmentResolver().resolve({
            "repo": "example/project",
            "python_version": "3.11",
            "dependencies": ["pytest==8.0.0"],
            "test_command": ["python", "-m", "pytest", "tests"],
            "environment_metadata": {"source": "fixture"},
        })
        self.assertEqual(spec.python_version, "3.11")
        self.assertEqual(spec.image, "vial-code-agent-swebench-python311:local")
        self.assertEqual(spec.dependencies, ("pytest==8.0.0",))
        self.assertEqual(spec.test_command[-1], "tests")
        self.assertEqual(spec.timeout_seconds, 900)
        self.assertEqual(dict(spec.metadata)["source"], "fixture")

    def test_environment_timeout_is_resolved_from_instance(self) -> None:
        spec = EnvironmentResolver().resolve({
            "repo": "example/project", "timeout_seconds": "120",
        })
        self.assertEqual(spec.timeout_seconds, 120)

    def test_astropy_environment_pins_its_build_and_test_dependencies(self) -> None:
        spec = EnvironmentResolver().resolve({"repo": "astropy/astropy"})
        self.assertIn("Cython<3", spec.dependencies)
        self.assertIn("pytest-astropy==0.9.0", spec.dependencies)
        self.assertIn("pytest-astropy-header==0.1.2", spec.dependencies)

    def test_baseline_empty_groups_keep_swebench_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = _run_test_groups(
                Path(directory), [], [], {}, None, (), (), 30)
        self.assertEqual(result[0], False)
        self.assertEqual(result[2], True)

    def test_independent_candidates_must_produce_the_same_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "value.txt"
            source.write_text("old\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            first = """--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
+new
"""
            second = first
            evidence = _candidate_consensus(
                root, first, second, {"value.txt"}, ("a/model", "b/model"))
            self.assertTrue(evidence["agreed"])
            self.assertEqual(source.read_text(encoding="utf-8"), "old\n")

            disagreement = _candidate_consensus(
                root, first, second.replace("+new", "+other"),
                {"value.txt"}, ("a/model", "b/model"))
            self.assertFalse(disagreement["agreed"])
            failed_behavior = _candidate_consensus(
                root, first, second, {"value.txt"}, ("a/model", "b/model"),
                behavioral={
                    "a/model": {"static_valid": True, "behavioral_passed": True},
                    "b/model": {"static_valid": True, "behavioral_passed": False},
                })
            self.assertFalse(failed_behavior["agreed"])

    def test_environment_failure_never_retries_model(self) -> None:
        self.assertFalse(should_retry_test_failure(
            "ModuleNotFoundError: pytest", "ModuleNotFoundError: pytest"))
        self.assertTrue(should_retry_test_failure(
            "assertion failed", "all tests passed"))
        self.assertTrue(baseline_is_valid(False, True))
        self.assertFalse(baseline_is_valid(True, True))

    def test_swebench_prompt_contains_instance_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("value = 1\n", encoding="utf-8")
            prompt = build_swebench_prompt(
                {"repo": "org/repo", "base_commit": "abc123",
                 "problem_statement": "Fix value", "fail_to_pass": ["tests/test.py::test_value"],
                 "pass_to_pass": ["tests/test.py::test_other"]},
                root, [source], {"source.py"},
                EnvironmentResolver().resolve({"repo": "org/repo", "python_version": "3.11"}),
            )
            self.assertIn("BASE COMMIT:\nabc123", prompt)
            self.assertIn("PYTHON:\n3.11", prompt)
            self.assertIn("CURRENT STATE: source.py", prompt)
            self.assertIn("FAIL_TO_PASS:", prompt)
            self.assertIn("Do not claim tests were executed.", prompt)
