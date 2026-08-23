import json
import unittest
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path

from benchmark.run_benchmark import classify_failure, summarize
from benchmark.run_swebench import (
    _failure_class, _failure_subclass, build_swebench_prompt, select_test_image,
    _adjudicated_candidate_consensus, _candidate_consensus,
    _candidate_outcome, _candidate_set_consensus, _generate_candidate_set,
    _generate_validated_candidate,
    _governed_apply,
    _run_test_groups, baseline_is_valid,
    should_retry_test_failure,
    success_metrics,
)
from benchmark.instance import InstanceSpec
from benchmark.report import candidate_metrics, success_metrics as report_success_metrics
from benchmark.swebench_environment import EnvironmentResolver


class BenchmarkMetricTests(unittest.TestCase):
    def test_candidate_metrics_do_not_turn_disagreement_into_candidate_failure(self) -> None:
        results = [{
            "passed": False,
            "consensus": {"agreed": False},
            "candidate_outcomes": {
                "candidate-a": {
                    "returned_patch": True, "patch_valid": True,
                    "tests_passed": True,
                },
                "candidate-b": {
                    "returned_patch": True, "patch_valid": True,
                    "tests_passed": True,
                    "attempts": 2, "retries": 1,
                },
            },
        }]

        metrics = candidate_metrics(results)

        self.assertEqual(metrics["candidate_attempts"], 3)
        self.assertEqual(metrics["candidate_retries"], 1)
        self.assertEqual(metrics["candidate_returned_patch"], 2)
        self.assertEqual(metrics["valid_patch"], 2)
        self.assertEqual(metrics["tests_passed"], 2)
        self.assertEqual(metrics["both_valid"], 1)
        self.assertEqual(metrics["agreement"], 0)
        self.assertEqual(metrics["candidate_completion_rate"], 2 / 3)
        self.assertEqual(metrics["candidate_patch_validity"], 1.0)
        self.assertEqual(metrics["candidate_behavioral_success"], 1.0)
        self.assertEqual(metrics["static_evidence"], 2)
        self.assertEqual(metrics["behavioral_evidence"], 2)
        self.assertEqual(metrics["complete_evidence"], 2)
        self.assertEqual(metrics["reliable_candidates"], 2)
        self.assertEqual(metrics["swebench_evidence_rate"], 1.0)
        self.assertEqual(metrics["candidate_behavioral_evidence_rate"], 1.0)
        self.assertEqual(metrics["candidate_reliability_rate"], 2 / 3)
        self.assertEqual(metrics["candidate_agreement"], 0.0)
        self.assertEqual(metrics["candidate_a_valid"], 1)
        self.assertEqual(metrics["candidate_b_valid"], 1)
        self.assertEqual(metrics["candidate_a_success_rate"], 1.0)
        self.assertEqual(metrics["candidate_b_success_rate"], 1.0)
        self.assertEqual(metrics["both_valid_rate"], 1.0)
        self.assertEqual(metrics["consensus_success"], 0)
        self.assertEqual(metrics["consensus_success_rate"], 0.0)

    def test_candidate_failure_breakdown_separates_contract_and_behavior(self) -> None:
        results = [{
            "candidate_outcomes": {
                "a/model": {
                    "candidate_id": "A", "returned_patch": False,
                    "response_received": True, "patch_valid": False,
                    "tests_passed": None, "failure_detail": "no patch",
                    "protocol": {"output": "unified_diff"},
                    "protocol_sha256": "protocol",
                    "workspace_sha256": "workspace",
                    "prompt_sha256": "same",
                },
                "b/model": {
                    "candidate_id": "B", "returned_patch": True,
                    "patch_valid": True, "tests_passed": False,
                    "failure_detail": "FAIL_TO_PASS assertion failed",
                    "protocol": {"output": "unified_diff"},
                    "protocol_sha256": "protocol",
                    "workspace_sha256": "workspace",
                    "prompt_sha256": "same",
                },
            }
        }]

        metrics = candidate_metrics(results)

        self.assertEqual(metrics["candidate_failure_breakdown"], {
            "A": {"no_patch": 1}, "B": {"behavioral_failure": 1}})
        self.assertEqual(metrics["protocol_parity"], 1)
        self.assertEqual(metrics["protocol_parity_tasks"], 1)
        self.assertEqual(metrics["diagnostic_table"]["valid"], {"A": 0, "B": 0})
        self.assertEqual(metrics["hash_parity"], {
            "tasks": 1, "prompt_equal": 1, "protocol_equal": 1,
            "workspace_equal": 1, "all_equal": 1, "known": 1,
        })

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
            self.assertEqual(disagreement["result_code"], "CONSENSUS_FAILED")
            self.assertEqual(
                disagreement["candidate_outcomes"]["a/model"]["pipeline"]["result"],
                "CANDIDATE_A_SUCCEEDED")
            self.assertEqual(
                disagreement["candidate_outcomes"]["b/model"]["pipeline"]["result"],
                "CANDIDATE_B_SUCCEEDED")
            behaviorally_equivalent = _candidate_consensus(
                root, first, second.replace("+new", "+other"),
                {"value.txt"}, ("a/model", "b/model"),
                behavioral={
                    "a/model": {"static_valid": True, "behavioral_passed": True},
                    "b/model": {"static_valid": True, "behavioral_passed": True},
                }, run_tests=True)
            self.assertFalse(behaviorally_equivalent["agreed"])
            self.assertEqual(behaviorally_equivalent["status"], "DISAGREEMENT")
            failed_behavior = _candidate_consensus(
                root, first, second, {"value.txt"}, ("a/model", "b/model"),
                behavioral={
                    "a/model": {"static_valid": True, "behavioral_passed": True},
                    "b/model": {"static_valid": True, "behavioral_passed": False},
                }, run_tests=True)
            self.assertFalse(failed_behavior["agreed"])

    def test_candidate_set_generation_does_not_short_circuit_after_a_failure(self) -> None:
        calls = []

        def generate(label, *args):
            calls.append(label)
            return {"model": label, "patch": None}

        results = _generate_candidate_set(
            [("A",), ("B",)], generate=generate)

        self.assertEqual(calls, ["A", "B"])
        self.assertEqual(len(results), 2)

    def test_candidate_generation_counts_internal_and_external_retries(self) -> None:
        generated = Mock()
        generated.patch = None
        generated.attempts = 3
        generated.failure_type = "no patch"
        agent = Mock()
        agent.generate.return_value = generated

        with unittest.mock.patch(
                "benchmark.run_swebench.CodeAgent", return_value=agent):
            candidate = _generate_validated_candidate(
                "A", "a/model", "prompt", Path("."), [], set(), Mock())

        self.assertEqual(candidate["outcome"]["attempts"], 6)
        self.assertEqual(candidate["outcome"]["retries"], 5)

    def test_one_valid_candidate_is_insufficient_not_consensus_failed(self) -> None:
        invalid = {
            "model": "a/model", "patch": None, "behavior": None,
            "outcome": _candidate_outcome(
                "A", "a/model", returned_patch=True, patch_valid=False,
                tests_passed=None, detail="invalid patch"),
        }
        valid = {
            "model": "b/model", "patch": "patch", "behavior": {
                "behavioral_passed": True},
            "outcome": _candidate_outcome(
                "B", "b/model", returned_patch=True, patch_valid=True,
                tests_passed=True),
        }

        consensus = _candidate_set_consensus(
            Path("."), [invalid, valid], set(), run_tests=True)

        self.assertFalse(consensus["agreed"])
        self.assertEqual(consensus["status"], "INSUFFICIENT_CANDIDATES")
        self.assertEqual(consensus["result_code"], "CANDIDATE_SET_INSUFFICIENT")
        self.assertEqual(set(consensus["candidate_outcomes"]),
                         {"a/model", "b/model"})

    def test_adjudicated_status_requires_second_passing_candidate(self) -> None:
        passing = {
            "model": "b/model", "patch": "patch-b", "behavior": {
                "behavioral_passed": True},
            "outcome": _candidate_outcome(
                "B", "b/model", returned_patch=True, patch_valid=True,
                tests_passed=True),
        }
        failing_adjudicator = {
            "model": "c/model", "patch": "patch-c", "behavior": {
                "behavioral_passed": False},
            "outcome": _candidate_outcome(
                "ADJUDICATOR", "c/model", returned_patch=True,
                patch_valid=True, tests_passed=False),
        }

        consensus = _adjudicated_candidate_consensus(
            Path("."), passing, failing_adjudicator, [passing], set())

        self.assertFalse(consensus["agreed"])
        self.assertNotEqual(consensus["status"], "ADJUDICATED")
        self.assertEqual(consensus["result_code"], "CANDIDATE_SET_INSUFFICIENT")

    def test_adjudicated_status_accepts_two_passing_equivalent_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "value.txt").write_text("old\n", encoding="utf-8")
            patch = """--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
+new
"""
            passing = {
                "model": "b/model", "patch": patch,
                "behavior": {"behavioral_passed": True},
                "outcome": _candidate_outcome(
                    "B", "b/model", returned_patch=True, patch_valid=True,
                    tests_passed=True),
            }
            adjudicator = {
                "model": "c/model", "patch": patch,
                "behavior": {"behavioral_passed": True},
                "outcome": _candidate_outcome(
                    "ADJUDICATOR", "c/model", returned_patch=True,
                    patch_valid=True, tests_passed=True),
            }

            consensus = _adjudicated_candidate_consensus(
                root, passing, adjudicator, [passing], {"value.txt"})

        self.assertTrue(consensus["agreed"])
        self.assertEqual(consensus["status"], "ADJUDICATED")

    def test_adjudicator_can_resolve_two_valid_candidates_in_disagreement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "value.txt").write_text("old\n", encoding="utf-8")
            first_patch = """--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
+first
"""
            second_patch = first_patch.replace("+first", "+second")
            candidates = [
                {
                    "model": "a/model", "patch": first_patch,
                    "behavior": {"behavioral_passed": True},
                    "outcome": _candidate_outcome(
                        "A", "a/model", returned_patch=True, patch_valid=True,
                        tests_passed=True),
                },
                {
                    "model": "b/model", "patch": second_patch,
                    "behavior": {"behavioral_passed": True},
                    "outcome": _candidate_outcome(
                        "B", "b/model", returned_patch=True, patch_valid=True,
                        tests_passed=True),
                },
            ]
            adjudicator = {
                "model": "c/model", "patch": second_patch,
                "behavior": {"behavioral_passed": True},
                "outcome": _candidate_outcome(
                    "ADJUDICATOR", "c/model", returned_patch=True,
                    patch_valid=True, tests_passed=True),
            }

            results = [_adjudicated_candidate_consensus(
                root, candidate, adjudicator, candidates, {"value.txt"})
                for candidate in candidates]

        self.assertFalse(results[0]["agreed"])
        self.assertTrue(results[1]["agreed"])
        self.assertEqual(results[1]["status"], "ADJUDICATED")

    def test_adjudicator_diagnostics_do_not_need_full_candidate_evidence(self) -> None:
        outcome = _candidate_outcome(
            "A", "a/model", returned_patch=True, patch_valid=True,
            tests_passed=False, detail="x" * 50000)
        diagnostics = {
            "result_code": outcome.get("result_code"),
            "pipeline": outcome.get("pipeline"),
            "behavioral_detail": ("x" * 50000)[-1500:],
        }

        self.assertLess(len(json.dumps(diagnostics)), 3000)
        self.assertNotIn("failure_detail", diagnostics)

    def test_consensus_requires_behavioral_evidence_when_tests_are_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "value.txt").write_text("old\n", encoding="utf-8")
            patch = """--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
+new
"""
            consensus = _candidate_consensus(
                root, patch, patch, {"value.txt"}, ("a/model", "b/model"),
                behavioral={"a/model": {"behavioral_passed": True}},
                run_tests=True)

        self.assertFalse(consensus["agreed"])
        self.assertEqual(consensus["result_code"], "CONSENSUS_FAILED")

    def test_candidate_b_failure_is_not_consensus_failure(self) -> None:
        outcome = _candidate_outcome(
            "B", "b/model", returned_patch=False, patch_valid=False,
            tests_passed=None, detail="empty response")

        self.assertEqual(outcome["result_code"], "CANDIDATE_B_FAILED")
        self.assertEqual(outcome["pipeline"]["patch"], "FAIL")
        self.assertEqual(outcome["pipeline"]["result"], "CANDIDATE_B_FAILED")

    def test_invalid_patch_retry_is_counted_as_a_candidate_retry(self) -> None:
        outcome = _candidate_outcome(
            "B", "b/model", returned_patch=True, patch_valid=False,
            tests_passed=None, detail="patch does not apply", attempts=2,
            retries=1, patch_returns=2)

        metrics = candidate_metrics([{"candidate_outcomes": {"b/model": outcome}}])

        self.assertEqual(metrics["candidate_attempts"], 2)
        self.assertEqual(metrics["candidate_retries"], 1)
        self.assertEqual(metrics["candidate_returned_patch"], 2)
        self.assertEqual(metrics["valid_patch"], 0)

    def test_governed_apply_persists_candidate_evidence(self) -> None:
        runtime = Mock()
        runtime.propose_patch_decision.return_value = SimpleNamespace(id="decision-1")
        result = Mock()
        result.ok.return_value = True
        result.metadata = {}
        runtime.apply_patch.return_value = result
        consensus = {
            "agreed": True,
            "agreement_ratio": 1.0,
            "models": ["a/model", "b/model"],
            "candidate_outcomes": {
                "a/model": {"attempts": 1, "retries": 0},
                "b/model": {"attempts": 2, "retries": 1},
            },
            "result_code": "CONSENSUS_SUCCEEDED",
        }

        _governed_apply(runtime, Path("."), "patch", "ctx", set(), consensus)

        evidence = runtime.record_consensus.call_args.kwargs["evidence"]
        self.assertEqual(evidence["candidate_outcomes"],
                         consensus["candidate_outcomes"])
        self.assertEqual(evidence["consensus_result"], {
            "result_code": "CONSENSUS_SUCCEEDED",
            "candidate_attempts": 3,
            "candidate_retries": 1,
        })

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
