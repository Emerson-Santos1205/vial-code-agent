import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from benchmark.aggregate_swebench import aggregate_reports
from benchmark.fetch_swebench import _tests
from benchmark.instance import InstanceSpec
from benchmark.report import (
    candidate_metrics,
    economics_metrics,
)
from benchmark.report import (
    success_metrics as report_success_metrics,
)
from benchmark.run_benchmark import classify_failure, summarize
from benchmark.run_swebench import (
    ASTROPY_BUILD_COMMAND,
    _adjudicated_candidate_consensus,
    _candidate_consensus,
    _candidate_outcome,
    _candidate_set_consensus,
    _failure_class,
    _failure_subclass,
    _generate_candidate_set,
    _generate_validated_candidate,
    _governed_apply,
    _normalize_astropy_test_id,
    _reverse_fixture,
    _run_test_groups,
    baseline_is_valid,
    build_swebench_prompt,
    is_prepared_test_image,
    resolution_summary,
    run_instance,
    select_shard,
    select_test_image,
    should_retry_test_failure,
    success_metrics,
    validate_environment_images,
)
from benchmark.swebench_environment import EnvironmentResolver
from benchmark.types import CandidateResult
from vial_code_agent.patches import PatchError


class BenchmarkMetricTests(unittest.TestCase):
    @patch("benchmark.run_swebench.subprocess.run")
    def test_environment_image_validation_reports_all_missing_images(self, run) -> None:
        run.return_value.returncode = 1
        with self.assertRaisesRegex(RuntimeError, "first.*second"):
            validate_environment_images({"second", "first"})
        self.assertEqual(run.call_count, 2)

    @patch("benchmark.run_swebench.subprocess.run")
    def test_environment_image_validation_returns_digest_reference(self, run) -> None:
        run.side_effect = [
            SimpleNamespace(returncode=0, stdout='["python@sha256:abc"]'),
        ]
        self.assertEqual(
            validate_environment_images({"python"}),
            {"python": "python@sha256:abc"},
        )

    @patch("benchmark.run_swebench.subprocess.run")
    def test_environment_image_validation_preserves_explicit_digest(self, run) -> None:
        run.return_value = SimpleNamespace(
            returncode=0, stdout='["registry.example/python@sha256:other"]')
        requested = "python:3.9@sha256:requested"
        self.assertEqual(validate_environment_images({requested}), {requested: requested})

    @patch("benchmark.run_swebench.subprocess.run")
    def test_environment_image_validation_returns_local_image_id(self, run) -> None:
        run.side_effect = [
            SimpleNamespace(returncode=0, stdout="[]"),
            SimpleNamespace(returncode=0, stdout="sha256:local"),
        ]
        self.assertEqual(
            validate_environment_images({"python:local"}),
            {"python:local": "python:local"},
        )

    def test_prepared_image_recognition_accepts_tags_and_digests(self) -> None:
        self.assertTrue(is_prepared_test_image(
            "vial-code-agent-swebench-python39:local"))
        self.assertTrue(is_prepared_test_image(
            "vial-code-agent-swebench-python39:local@sha256:abc"))
        self.assertFalse(is_prepared_test_image(
            "swebench/sweb.eval.x86_64.task:latest@sha256:abc"))
        self.assertFalse(is_prepared_test_image(None))

    def test_aggregate_reports_rejects_duplicate_tasks(self) -> None:
        report = {
            "benchmark": "verified", "execution": {
                "model": "openai/gpt-4o", "adapters": ["vial"]},
            "results": [{"id": "task-1", "adapter": "vial", "passed": True,
                          "environment_valid": True}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate result"):
                aggregate_reports([path, path])

    def test_aggregate_reports_groups_adapters(self) -> None:
        report = {
            "benchmark": "verified", "execution": {
                "model": "openai/gpt-4o", "adapters": ["baseline", "vial"]},
            "results": [
                {"id": "task-2", "adapter": "vial", "passed": True,
                 "environment_valid": True, "duration_seconds": 2},
                {"id": "task-1", "adapter": "baseline", "passed": False,
                 "environment_valid": True, "duration_seconds": 1},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            result = aggregate_reports([path])
        self.assertEqual(result["tasks"], 2)
        self.assertEqual(result["by_adapter"]["vial"]["metrics"]["agent_solved"], 1)
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

    def test_resolution_never_declares_partial_work_as_100_percent(self) -> None:
        self.assertEqual(
            resolution_summary([{"passed": True}], 2),
            {
                "percent": 50.0,
                "is_100_percent": False,
                "definition": ("100% is declared only when every selected "
                               "instance and adapter result has passed=True."),
            })
        self.assertTrue(resolution_summary(
            [{"passed": True}, {"passed": True}], 2)["is_100_percent"])

    def test_economics_uses_inference_tokens_and_all_attempted_candidates(self) -> None:
        results = [
            {"passed": True, "duration_seconds": 10, "candidate_outcomes": {
                "a/model": {"input_tokens": 100, "output_tokens": 20},
                "b/model": {"input_tokens": 80, "output_tokens": 10},
            }},
            {"passed": False, "duration_seconds": 30, "candidate_outcomes": {
                "a/model": {"input_tokens": 50, "output_tokens": 5},
            }},
        ]

        metrics = economics_metrics(results)

        self.assertEqual(metrics["total_tokens"], 265)
        self.assertEqual(metrics["tokens_per_resolved_task"], 265)
        self.assertEqual(metrics["seconds_per_resolved_task"], 40)
        self.assertEqual(metrics["sample_size_status"], "diagnostic_only")

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

    def test_astropy_setup_always_builds_current_extensions(self) -> None:
        self.assertEqual(ASTROPY_BUILD_COMMAND,
                         "python setup.py build_ext --inplace")

    def test_shards_are_balanced_and_cover_the_requested_range_once(self) -> None:
        tasks = [{"id": str(index)} for index in range(10)]
        shards = [select_shard(tasks, 1, 8, shard, 3) for shard in range(3)]

        self.assertEqual([[index for index, _ in shard] for shard in shards],
                         [[1, 2], [3, 4, 5], [6, 7, 8]])
        self.assertEqual([item["id"] for shard in shards for _, item in shard],
                         [str(index) for index in range(1, 9)])
        with self.assertRaises(ValueError):
            select_shard(tasks, 0, 1, 1, 1)

    def test_swebench_rejects_unknown_adapter_before_creating_a_workspace(self) -> None:
        with self.assertRaises(ValueError):
            run_instance({"id": "task"}, "model", adapter="unknown")

    def test_preflight_rejects_unknown_adapter_before_creating_a_workspace(self) -> None:
        with self.assertRaises(ValueError):
            run_instance({"id": "task"}, "model", adapter="unknown",
                         preflight_only=True)

    def test_fetch_normalizes_json_encoded_test_lists(self) -> None:
        self.assertEqual(_tests('["tests/test_a.py::test_a"]'),
                         ["tests/test_a.py::test_a"])
        self.assertEqual(_tests("plain test"), "plain test")

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
        self.assertEqual(len(spec.fingerprint), 64)

    def test_environment_fingerprint_changes_with_effective_contract(self) -> None:
        resolver = EnvironmentResolver()
        first = resolver.resolve({"repo": "example/project", "python_version": "3.11"})
        second = resolver.resolve({"repo": "example/project", "python_version": "3.12"})
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_environment_contract_normalizes_dependencies_and_revision(self) -> None:
        resolver = EnvironmentResolver()
        first = resolver.resolve({
            "repo": "example/project", "base_commit": "abc",
            "dependencies": ["wheel", "pytest==8.0.0", "wheel"],
        })
        second = resolver.resolve({
            "repo": "example/project", "base_commit": "abc",
            "dependencies": ["pytest==8.0.0", "wheel"],
        })
        changed = resolver.resolve({
            "repo": "example/project", "base_commit": "def",
            "dependencies": ["pytest==8.0.0", "wheel"],
        })
        self.assertEqual(first.dependencies, ("pytest==8.0.0", "wheel"))
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertNotEqual(first.fingerprint, changed.fingerprint)
        self.assertEqual(dict(first.metadata)["catalog_key"], "example/project@abc")

    def test_environment_spec_splits_string_test_command(self) -> None:
        spec = EnvironmentResolver().resolve({
            "repo": "example/project",
            "test_command": "python -m pytest tests/test_api.py",
        })
        self.assertEqual(spec.test_command,
                         ("python", "-m", "pytest", "tests/test_api.py"))

    def test_environment_spec_rejects_invalid_python_version(self) -> None:
        with self.assertRaises(ValueError):
            EnvironmentResolver().resolve({"python_version": "latest"})

    def test_environment_timeout_is_resolved_from_instance(self) -> None:
        spec = EnvironmentResolver().resolve({
            "repo": "example/project", "timeout_seconds": "120",
        })
        self.assertEqual(spec.timeout_seconds, 120)

    def test_historical_requests_uses_python27(self) -> None:
        spec = EnvironmentResolver().resolve({"repo": "psf/requests"})
        self.assertEqual(spec.python_version, "2.7")
        self.assertEqual(spec.image, "vial-code-agent-swebench-python27:local")

    def test_astropy_environment_allows_initial_extension_build(self) -> None:
        spec = EnvironmentResolver().resolve({"repo": "astropy/astropy"})
        self.assertEqual(spec.timeout_seconds, 1800)

    def test_astropy_parametrized_test_ids_are_normalized_for_custom_runner(self) -> None:
        self.assertEqual(
            _normalize_astropy_test_id(
                "astropy/table/tests/test_table.py::test_case[param]"),
            "astropy/table/tests/test_table.py::test_case")
        self.assertEqual(
            _normalize_astropy_test_id(
                "astropy/table/tests/test_table.py::test_case[broken"),
            "astropy/table/tests/test_table.py::test_case")

    def test_astropy_environment_pins_its_build_and_test_dependencies(self) -> None:
        spec = EnvironmentResolver().resolve({"repo": "astropy/astropy"})
        self.assertIn("Cython<3", spec.dependencies)
        self.assertIn("pytest-astropy==0.9.0", spec.dependencies)
        self.assertIn("pytest-astropy-header==0.1.2", spec.dependencies)

    def test_prepared_image_installs_declared_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "astropy").mkdir()
            captured = {}
            with patch("benchmark.run_swebench._run_command") as run_command:
              with patch("benchmark.run_swebench._prepare_astropy_extensions",
                         return_value=(True, "prepared")):
                def capture(command, *args, **kwargs):
                    captured["script"] = (root / ".vial-test-groups.sh").read_text()
                    return SimpleNamespace(
                        returncode=0,
                        stdout="__VIAL_FAIL_BEGIN__\n__VIAL_FAIL_END__:0\n"
                               "__VIAL_PASS_BEGIN__\n__VIAL_PASS_END__:0\n",
                        stderr="",
                    )
                run_command.side_effect = capture
                _run_test_groups(root, [], [], {},
                                 "vial-code-agent-swebench-python39:local@sha256:abc",
                                 ("pytest==7.4.4",), (), 30)
            self.assertIn("python -m pip install pytest==7.4.4", captured["script"])
            self.assertNotIn("pip install -e", captured["script"])

    def test_configured_pytest_command_is_scoped_to_each_test_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captured = {}
            with patch("benchmark.run_swebench._run_command") as run_command:
                def capture(command, *args, **kwargs):
                    captured["script"] = (root / ".vial-test-groups.sh").read_text()
                    return SimpleNamespace(
                        returncode=0,
                        stdout="__VIAL_FAIL_BEGIN__\n__VIAL_FAIL_END__:0\n"
                               "__VIAL_PASS_BEGIN__\n__VIAL_PASS_END__:0\n",
                        stderr="",
                    )
                run_command.side_effect = capture
                _run_test_groups(root, ["tests/test_api.py::test_fix"],
                                 ["tests/test_api.py::test_other"], {},
                                 "python:3.9",
                                 (), ("python", "-m", "pytest", "-q"), 30)
            self.assertIn("tests/test_api.py::test_fix", captured["script"])
            self.assertIn("tests/test_api.py::test_other", captured["script"])

    def test_fixture_rollback_failure_is_reported(self) -> None:
        with patch("benchmark.run_swebench.PatchApplier") as applier:
            applier.return_value.reverse.side_effect = PatchError("rollback failed")
            restored, detail = _reverse_fixture(Path("."), "fixture")
        self.assertFalse(restored)
        self.assertIn("rollback failed", detail)

    def test_astropy_runner_disables_incompatible_header_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "astropy").mkdir()
            captured = {}
            with patch("benchmark.run_swebench._run_command") as run_command:
              with patch("benchmark.run_swebench._prepare_astropy_extensions",
                         return_value=(True, "prepared")):
                def capture(command, *args, **kwargs):
                    captured["script"] = (root / ".vial-test-groups.sh").read_text()
                    return SimpleNamespace(returncode=0, stdout="", stderr="")

                run_command.side_effect = capture
                _run_test_groups(root, ["astropy/tests/test_one.py"], [], {},
                                 "vial-code-agent-swebench-python39:local", (), (), 30)
            self.assertIn("-p no:astropy_header", captured["script"])

    def test_baseline_empty_groups_keep_swebench_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = _run_test_groups(
                Path(directory), [], [], {}, None, (), (), 30)
        self.assertEqual(result[0], False)
        self.assertEqual(result[2], True)

    def test_provider_failures_have_a_distinct_failure_class(self) -> None:
        self.assertEqual(_failure_class("provider", "empty response"), "provider")
        self.assertEqual(_failure_subclass(
            "provider", "model returned an empty response", {"failure_class": "provider"}),
            "model_response")

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
            self.assertTrue(evidence.agreed)
            self.assertEqual(source.read_text(encoding="utf-8"), "old\n")

            disagreement = _candidate_consensus(
                root, first, second.replace("+new", "+other"),
                {"value.txt"}, ("a/model", "b/model"))
            self.assertFalse(disagreement.agreed)
            self.assertEqual(disagreement.result_code, "CONSENSUS_FAILED")
            self.assertEqual(
                disagreement.candidate_outcomes["a/model"].to_dict()["pipeline"]["result"],
                "CANDIDATE_A_SUCCEEDED")
            self.assertEqual(
                disagreement.candidate_outcomes["b/model"].to_dict()["pipeline"]["result"],
                "CANDIDATE_B_SUCCEEDED")
            behaviorally_equivalent = _candidate_consensus(
                root, first, second.replace("+new", "+other"),
                {"value.txt"}, ("a/model", "b/model"),
                behavioral={
                    "a/model": {"static_valid": True, "behavioral_passed": True},
                    "b/model": {"static_valid": True, "behavioral_passed": True},
                }, run_tests=True)
            self.assertFalse(behaviorally_equivalent.agreed)
            self.assertEqual(behaviorally_equivalent.status, "DISAGREEMENT")
            failed_behavior = _candidate_consensus(
                root, first, second, {"value.txt"}, ("a/model", "b/model"),
                behavioral={
                    "a/model": {"static_valid": True, "behavioral_passed": True},
                    "b/model": {"static_valid": True, "behavioral_passed": False},
                }, run_tests=True)
            self.assertFalse(failed_behavior.agreed)

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

        self.assertEqual(candidate.outcome.attempts, 6)
        self.assertEqual(candidate.outcome.retries, 5)

    def test_one_valid_candidate_is_insufficient_not_consensus_failed(self) -> None:
        invalid = CandidateResult(
            model="a/model", patch=None, behavior=None,
            outcome=_candidate_outcome(
                "A", "a/model", returned_patch=True, patch_valid=False,
                tests_passed=None, detail="invalid patch"),
        )
        valid = CandidateResult(
            model="b/model", patch="patch", behavior={"behavioral_passed": True},
            outcome=_candidate_outcome(
                "B", "b/model", returned_patch=True, patch_valid=True,
                tests_passed=True),
        )

        consensus = _candidate_set_consensus(
            Path("."), [invalid, valid], set(), run_tests=True)

        self.assertFalse(consensus.agreed)
        self.assertEqual(consensus.status, "INSUFFICIENT_CANDIDATES")
        self.assertEqual(consensus.result_code, "CANDIDATE_SET_INSUFFICIENT")
        self.assertEqual(set(consensus.candidate_outcomes),
                         {"a/model", "b/model"})

    def test_adjudicated_status_requires_second_passing_candidate(self) -> None:
        passing = CandidateResult(
            model="b/model", patch="patch-b", behavior={"behavioral_passed": True},
            outcome=_candidate_outcome(
                "B", "b/model", returned_patch=True, patch_valid=True,
                tests_passed=True),
        )
        failing_adjudicator = CandidateResult(
            model="c/model", patch="patch-c", behavior={"behavioral_passed": False},
            outcome=_candidate_outcome(
                "ADJUDICATOR", "c/model", returned_patch=True,
                patch_valid=True, tests_passed=False),
        )

        consensus = _adjudicated_candidate_consensus(
            Path("."), passing, failing_adjudicator, [passing], set())

        self.assertFalse(consensus.agreed)
        self.assertNotEqual(consensus.status, "ADJUDICATED")
        self.assertEqual(consensus.result_code, "CANDIDATE_SET_INSUFFICIENT")

    def test_adjudicated_status_accepts_two_passing_equivalent_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "value.txt").write_text("old\n", encoding="utf-8")
            patch = "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-old\n+new\n"
            passing = CandidateResult(
                model="b/model", patch=patch,
                behavior={"behavioral_passed": True},
                outcome=_candidate_outcome(
                    "B", "b/model", returned_patch=True, patch_valid=True,
                    tests_passed=True),
            )
            adjudicator = CandidateResult(
                model="c/model", patch=patch,
                behavior={"behavioral_passed": True},
                outcome=_candidate_outcome(
                    "ADJUDICATOR", "c/model", returned_patch=True,
                    patch_valid=True, tests_passed=True),
            )

            consensus = _adjudicated_candidate_consensus(
                root, passing, adjudicator, [passing], {"value.txt"})

        self.assertTrue(consensus.agreed)
        self.assertEqual(consensus.status, "ADJUDICATED")

    def test_adjudicator_can_resolve_two_valid_candidates_in_disagreement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "value.txt").write_text("old\n", encoding="utf-8")
            first_patch = "--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-old\n+first\n"
            second_patch = first_patch.replace("+first", "+second")
            candidates = [
                CandidateResult(
                    model="a/model", patch=first_patch,
                    behavior={"behavioral_passed": True},
                    outcome=_candidate_outcome(
                        "A", "a/model", returned_patch=True, patch_valid=True,
                        tests_passed=True),
                ),
                CandidateResult(
                    model="b/model", patch=second_patch,
                    behavior={"behavioral_passed": True},
                    outcome=_candidate_outcome(
                        "B", "b/model", returned_patch=True, patch_valid=True,
                        tests_passed=True),
                ),
            ]
            adjudicator = CandidateResult(
                model="c/model", patch=second_patch,
                behavior={"behavioral_passed": True},
                outcome=_candidate_outcome(
                    "ADJUDICATOR", "c/model", returned_patch=True,
                    patch_valid=True, tests_passed=True),
            )

            results = [_adjudicated_candidate_consensus(
                root, candidate, adjudicator, candidates, {"value.txt"})
                for candidate in candidates]

        self.assertFalse(results[0].agreed)
        self.assertTrue(results[1].agreed)
        self.assertEqual(results[1].status, "ADJUDICATED")

    def test_adjudicator_diagnostics_do_not_need_full_candidate_evidence(self) -> None:
        outcome = _candidate_outcome(
            "A", "a/model", returned_patch=True, patch_valid=True,
            tests_passed=False, detail="x" * 50000)
        diagnostics = {
            "result_code": outcome.result_code,
            "pipeline": outcome.to_dict()["pipeline"],
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

        self.assertFalse(consensus.agreed)
        self.assertEqual(consensus.result_code, "CONSENSUS_FAILED")

    def test_candidate_b_failure_is_not_consensus_failure(self) -> None:
        outcome = _candidate_outcome(
            "B", "b/model", returned_patch=False, patch_valid=False,
            tests_passed=None, detail="empty response")

        self.assertEqual(outcome.result_code, "CANDIDATE_B_FAILED")
        self.assertEqual(outcome.pipeline.patch, "FAIL")
        self.assertEqual(outcome.pipeline.result, "CANDIDATE_B_FAILED")

    def test_invalid_patch_retry_is_counted_as_a_candidate_retry(self) -> None:
        outcome = _candidate_outcome(
            "B", "b/model", returned_patch=True, patch_valid=False,
            tests_passed=None, detail="patch does not apply", attempts=2,
            retries=1, patch_returns=2)

        metrics = candidate_metrics([{"candidate_outcomes": {"b/model": outcome.to_dict()}}])

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
        self.assertFalse(baseline_is_valid(
            False, True, "No matching distribution found for pytest==7.4.4"))

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
