"""Tests for benchmark/run_swebench.py core functions."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from benchmark.run_swebench import (
    _adjudicated_candidate_consensus,
    _annotate_result,
    _as_tests,
    _candidate_consensus,
    _candidate_outcome,
    _candidate_set_consensus,
    _failure_class,
    _failure_subclass,
    _generate_candidate_set,
    _governed_apply,
    _token_count,
    baseline_is_valid,
    build_swebench_prompt,
    changed_paths,
    resolution_summary,
    select_shard,
    should_retry_test_failure,
    success_metrics,
)
from benchmark.types import (
    CandidateConsensus,
    CandidateOutcome,
    CandidateResult,
)


class CandidateOutcomeTests(unittest.TestCase):
    def test_to_dict_roundtrip(self) -> None:
        outcome = _candidate_outcome(
            "A", "a/model", returned_patch=True, patch_valid=True,
            tests_passed=True, detail="ok", attempts=3, retries=1,
            patch_returns=2)
        d = outcome.to_dict()
        self.assertEqual(d["candidate_id"], "A")
        self.assertEqual(d["model"], "a/model")
        self.assertTrue(d["returned_patch"])
        self.assertTrue(d["patch_valid"])
        self.assertTrue(d["tests_passed"])
        self.assertEqual(d["attempts"], 3)
        self.assertEqual(d["retries"], 1)
        self.assertEqual(d["patch_returns"], 2)
        restored = CandidateOutcome.from_dict(d)
        self.assertEqual(restored.candidate_id, "A")
        self.assertTrue(restored.tests_passed)

    def test_pipeline_stages(self) -> None:
        outcome = _candidate_outcome(
            "B", "b/model", returned_patch=False, patch_valid=False,
            tests_passed=None)
        self.assertEqual(outcome.pipeline.patch, "FAIL")
        self.assertEqual(outcome.pipeline.static, "FAIL")
        self.assertEqual(outcome.pipeline.behavioral, "NOT_RUN")
        self.assertIn("FAILED", outcome.result_code)

    def test_success_result_code(self) -> None:
        outcome = _candidate_outcome(
            "A", "a/model", returned_patch=True, patch_valid=True,
            tests_passed=True)
        self.assertEqual(outcome.result_code, "CANDIDATE_A_SUCCEEDED")

    def test_optional_fields_omitted_when_empty(self) -> None:
        outcome = _candidate_outcome(
            "A", "a/model", returned_patch=True, patch_valid=True,
            tests_passed=True)
        d = outcome.to_dict()
        self.assertNotIn("input_tokens", d)
        self.assertNotIn("failure_stage", d)


class CandidateConsensusTests(unittest.TestCase):
    def test_to_dict_roundtrip(self) -> None:
        c = CandidateConsensus(
            agreed=True, agreement_ratio=1.0,
            models=["a", "b"], responses={"a": "p1", "b": "p2"},
            evidence={"comparison": "ok"},
            status="APPROVED", result_code="CONSENSUS_SUCCEEDED",
            note="test")
        d = c.to_dict()
        self.assertTrue(d["agreed"])
        self.assertEqual(d["models"], ["a", "b"])
        restored = CandidateConsensus.from_dict(d)
        self.assertTrue(restored.agreed)
        self.assertEqual(restored.models, ["a", "b"])


class CandidateResultTests(unittest.TestCase):
    def test_to_dict(self) -> None:
        outcome = _candidate_outcome(
            "A", "a/model", returned_patch=True, patch_valid=True,
            tests_passed=True)
        cr = CandidateResult(
            model="a/model", patch="diff", generated=None,
            outcome=outcome, behavior={"behavioral_passed": True})
        d = cr.to_dict()
        self.assertEqual(d["model"], "a/model")
        self.assertEqual(d["patch"], "diff")
        self.assertTrue(d["behavior"]["behavioral_passed"])
        self.assertEqual(d["outcome"]["candidate_id"], "A")


class FailureClassTests(unittest.TestCase):
    def test_environment_stages(self) -> None:
        for stage in ("clone", "checkout", "test_environment",
                      "test_fixture", "test_selection", "baseline_tests"):
            self.assertEqual(_failure_class(stage), "environment", stage)

    def test_patch_stages(self) -> None:
        for stage in ("patch_contract", "patch_validation", "patch_apply",
                      "test_retry_revert", "test_retry_contract",
                      "test_retry_patch"):
            self.assertEqual(_failure_class(stage), "patch", stage)

    def test_governance_stage(self) -> None:
        self.assertEqual(_failure_class("governance"), "governance")

    def test_provider_stages(self) -> None:
        for stage in ("provider", "provider_health"):
            self.assertEqual(_failure_class(stage), "provider", stage)

    def test_tests_with_environment_markers(self) -> None:
        detail = "ModuleNotFoundError: No module named 'foo'"
        self.assertEqual(_failure_class("tests", detail), "environment")

    def test_tests_without_environment_markers(self) -> None:
        detail = "AssertionError: expected 42 got 0"
        self.assertEqual(_failure_class("tests", detail), "tests")

    def test_unknown_stage(self) -> None:
        self.assertEqual(_failure_class("something_else"), "unknown")


class FailureSubclassTests(unittest.TestCase):
    def test_timeout_in_tests(self) -> None:
        self.assertEqual(
            _failure_subclass("tests", "command timed out"), "timeout")

    def test_regression(self) -> None:
        result = {"pass_to_pass": False, "fail_to_pass": True}
        self.assertEqual(
            _failure_subclass("tests", "some error", result), "regression")

    def test_wrong_solution(self) -> None:
        result = {"pass_to_pass": True, "fail_to_pass": True}
        self.assertEqual(
            _failure_subclass("tests", "some error", result), "wrong_solution")

    def test_no_op_in_patch_contract(self) -> None:
        self.assertEqual(
            _failure_subclass("patch_contract", "no-op detected"), "no_op")

    def test_context_mismatch(self) -> None:
        self.assertEqual(
            _failure_subclass("patch_contract", "does not apply"),
            "context_mismatch")

    def test_provider_timeout(self) -> None:
        result = {"failure_class": "provider"}
        self.assertEqual(
            _failure_subclass("provider", "request timed out", result), "infrastructure")

    def test_provider_auth(self) -> None:
        self.assertEqual(
            _failure_subclass("provider", "401 unauthorized"),
            "authentication")


class ShouldRetryTests(unittest.TestCase):
    def test_environment_failure_no_retry(self) -> None:
        self.assertFalse(should_retry_test_failure(
            "ModuleNotFoundError", "ok"))

    def test_test_failure_with_retry(self) -> None:
        self.assertTrue(should_retry_test_failure(
            "AssertionError: expected 1", "ok"))


class BaselineIsValidTests(unittest.TestCase):
    def test_valid_baseline(self) -> None:
        self.assertTrue(baseline_is_valid(
            fail_to_pass=False, pass_to_pass=True,
            fail_detail="test_fix failed", pass_detail="all pass"))

    def test_invalid_when_fail_passes(self) -> None:
        self.assertFalse(baseline_is_valid(
            fail_to_pass=True, pass_to_pass=True,
            fail_detail="test_fix passed", pass_detail="all pass"))

    def test_invalid_when_pass_fails(self) -> None:
        self.assertFalse(baseline_is_valid(
            fail_to_pass=False, pass_to_pass=False,
            fail_detail="test_fix failed", pass_detail="some regression"))


class AsTestsTests(unittest.TestCase):
    def test_string_input(self) -> None:
        result = _as_tests("test_foo\ntest_bar")
        self.assertEqual(result, ["test_foo", "test_bar"])

    def test_list_input(self) -> None:
        result = _as_tests(["test_foo", "test_bar"])
        self.assertEqual(result, ["test_foo", "test_bar"])

    def test_normalizes_parens(self) -> None:
        result = _as_tests(["test_foo (MyClass)"])
        self.assertEqual(result, ["MyClass.test_foo"])

    def test_empty_input(self) -> None:
        self.assertEqual(_as_tests(None), [])
        self.assertEqual(_as_tests(""), [])


class TokenCountTests(unittest.TestCase):
    def test_valid_int(self) -> None:
        self.assertEqual(_token_count(42), 42)

    def test_none(self) -> None:
        self.assertEqual(_token_count(None), 0)

    def test_string(self) -> None:
        self.assertEqual(_token_count("abc"), 0)


class ChangedPathsTests(unittest.TestCase):
    def test_extracts_b_prefix(self) -> None:
        patch = "--- a/foo.py\n+++ b/bar.py\n@@ -1 +1 @@\n-old\n+new"
        self.assertEqual(changed_paths(patch), ["bar.py"])

    def test_dev_null(self) -> None:
        patch = "--- /dev/null\n+++ b/new.py\n@@ -0 +1 @@\n+new"
        self.assertEqual(changed_paths(patch), ["new.py"])

    def test_deduplicates(self) -> None:
        patch = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n--- a/x.py\n+++ b/x.py"
        self.assertEqual(changed_paths(patch), ["x.py"])


class SelectShardTests(unittest.TestCase):
    def test_full_range(self) -> None:
        tasks = [{"id": i} for i in range(10)]
        result = select_shard(tasks, 0, 10, 0, 1)
        self.assertEqual(len(result), 10)

    def test_shard_split(self) -> None:
        tasks = [{"id": i} for i in range(10)]
        shard0 = select_shard(tasks, 0, 10, 0, 2)
        shard1 = select_shard(tasks, 0, 10, 1, 2)
        self.assertEqual(len(shard0), 5)
        self.assertEqual(len(shard1), 5)
        ids0 = {idx for idx, _ in shard0}
        ids1 = {idx for idx, _ in shard1}
        self.assertEqual(ids0 | ids1, set(range(10)))

    def test_invalid_shard_count(self) -> None:
        with self.assertRaises(ValueError):
            select_shard([], 0, 0, 0, 0)

    def test_invalid_shard_index(self) -> None:
        with self.assertRaises(ValueError):
            select_shard([], 0, 0, 5, 3)


class ResolutionSummaryTests(unittest.TestCase):
    def test_100_percent(self) -> None:
        results = [{"passed": True}, {"passed": True}]
        r = resolution_summary(results, 2)
        self.assertTrue(r["is_100_percent"])
        self.assertEqual(r["percent"], 100.0)

    def test_not_100_percent(self) -> None:
        results = [{"passed": True}, {"passed": False}]
        r = resolution_summary(results, 2)
        self.assertFalse(r["is_100_percent"])

    def test_empty_expected(self) -> None:
        r = resolution_summary([], 0)
        self.assertFalse(r["is_100_percent"])


class SuccessMetricsTests(unittest.TestCase):
    def test_basic_metrics(self) -> None:
        results = [
            {"passed": True, "failure_class": "none"},
            {"passed": False, "failure_class": "tests"},
            {"passed": False, "failure_class": "environment"},
        ]
        m = success_metrics(results)
        self.assertEqual(m["tasks"], 3)
        self.assertEqual(m["agent_solved"], 1)
        self.assertEqual(m["environment_valid"], 2)
        self.assertAlmostEqual(m["agent_success_rate"], 0.5)

    def test_candidate_metrics_included(self) -> None:
        results = [
            {"passed": True, "failure_class": "none",
             "candidate_outcomes": {
                 "a/model": _candidate_outcome(
                     "A", "a/model", returned_patch=True, patch_valid=True,
                     tests_passed=True).to_dict(),
             }},
        ]
        m = success_metrics(results)
        self.assertIn("candidate_attempts", m)


class CandidateOutcomeFunctionTests(unittest.TestCase):
    def test_all_fields_set(self) -> None:
        outcome = _candidate_outcome(
            "A", "m", returned_patch=True, patch_valid=True,
            tests_passed=True, detail="d", attempts=2, retries=1,
            patch_returns=3)
        self.assertEqual(outcome.candidate_id, "A")
        self.assertEqual(outcome.model, "m")
        self.assertTrue(outcome.returned_patch)
        self.assertTrue(outcome.patch_valid)
        self.assertTrue(outcome.tests_passed)
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(outcome.retries, 1)
        self.assertEqual(outcome.patch_returns, 3)
        self.assertEqual(outcome.failure_detail, "d")

    def test_no_patch(self) -> None:
        outcome = _candidate_outcome(
            "B", "m", returned_patch=False, patch_valid=False,
            tests_passed=None)
        self.assertFalse(outcome.returned_patch)
        self.assertEqual(outcome.pipeline.patch, "FAIL")
        self.assertEqual(outcome.result_code, "CANDIDATE_B_FAILED")


class CandidateConsensusFunctionTests(unittest.TestCase):
    def test_equivalent_patches_agree(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "f.txt").write_text("old\n", encoding="utf-8")
            patch = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-old\n+new\n"
            c = _candidate_consensus(
                root, patch, patch, {"f.txt"}, ("a", "b"))
            self.assertTrue(c.agreed)
            self.assertEqual(c.status, "APPROVED")

    def test_different_patches_disagree(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "f.txt").write_text("old\n", encoding="utf-8")
            p1 = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-old\n+new1\n"
            p2 = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-old\n+new2\n"
            c = _candidate_consensus(
                root, p1, p2, {"f.txt"}, ("a", "b"))
            self.assertFalse(c.agreed)
            self.assertEqual(c.status, "DISAGREEMENT")

    def test_behavioral_flag(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "f.txt").write_text("old\n", encoding="utf-8")
            patch = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-old\n+new\n"
            c = _candidate_consensus(
                root, patch, patch, {"f.txt"}, ("a", "b"),
                behavioral={"a": {"behavioral_passed": True},
                             "b": {"behavioral_passed": False}},
                run_tests=True)
            self.assertFalse(c.agreed)


class CandidateSetConsensusTests(unittest.TestCase):
    def test_insufficient_candidates(self) -> None:
        c = CandidateResult(
            model="a", patch=None,
            outcome=_candidate_outcome(
                "A", "a", returned_patch=False, patch_valid=False,
                tests_passed=None))
        consensus = _candidate_set_consensus(Path("."), [c], set(), False)
        self.assertEqual(consensus.status, "INSUFFICIENT_CANDIDATES")

    def test_two_valid_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "f.txt").write_text("old\n", encoding="utf-8")
            patch = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-old\n+new\n"
            cr1 = CandidateResult(
                model="a", patch=patch,
                outcome=_candidate_outcome(
                    "A", "a", returned_patch=True, patch_valid=True,
                    tests_passed=True))
            cr2 = CandidateResult(
                model="b", patch=patch,
                outcome=_candidate_outcome(
                    "B", "b", returned_patch=True, patch_valid=True,
                    tests_passed=True))
            consensus = _candidate_set_consensus(
                root, [cr1, cr2], {"f.txt"}, False)
            self.assertTrue(consensus.agreed)


class AdjudicatedConsensusTests(unittest.TestCase):
    def test_adjudicator_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "f.txt").write_text("old\n", encoding="utf-8")
            patch = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-old\n+new\n"
            passing = CandidateResult(
                model="a", patch=patch,
                behavior={"behavioral_passed": True},
                outcome=_candidate_outcome(
                    "A", "a", returned_patch=True, patch_valid=True,
                    tests_passed=True))
            adjudicator = CandidateResult(
                model="b", patch=patch,
                behavior={"behavioral_passed": True},
                outcome=_candidate_outcome(
                    "ADJUDICATOR", "b", returned_patch=True,
                    patch_valid=True, tests_passed=True))
            c = _adjudicated_candidate_consensus(
                root, passing, adjudicator, [passing], {"f.txt"})
            self.assertTrue(c.agreed)
            self.assertEqual(c.status, "ADJUDICATED")


class GovernedApplyTests(unittest.TestCase):
    def test_applies_with_consensus_dict(self) -> None:
        runtime = Mock()
        runtime.propose_patch_decision.return_value = SimpleNamespace(id="d1")
        result = Mock()
        result.ok.return_value = True
        result.metadata = {}
        runtime.apply_patch.return_value = result
        consensus = {"agreed": True, "result_code": "OK",
                     "evidence": {}, "models": [], "responses": {}}
        ok, err, meta = _governed_apply(
            runtime, Path("."), "patch", "ctx", set(), consensus=consensus)
        self.assertTrue(ok)
        runtime.record_consensus.assert_called_once()

    def test_applies_with_candidate_consensus(self) -> None:
        runtime = Mock()
        runtime.propose_patch_decision.return_value = SimpleNamespace(id="d1")
        result = Mock()
        result.ok.return_value = True
        result.metadata = {}
        runtime.apply_patch.return_value = result
        consensus = CandidateConsensus(
            agreed=True, result_code="OK", evidence={}, models=[], responses={})
        ok, err, meta = _governed_apply(
            runtime, Path("."), "patch", "ctx", set(), consensus=consensus)
        self.assertTrue(ok)

    def test_no_consensus(self) -> None:
        runtime = Mock()
        runtime.propose_patch_decision.return_value = SimpleNamespace(id="d1")
        result = Mock()
        result.ok.return_value = True
        result.metadata = {}
        runtime.apply_patch.return_value = result
        ok, err, meta = _governed_apply(
            runtime, Path("."), "patch", "ctx", set(), consensus=None)
        self.assertTrue(ok)
        runtime.record_consensus.assert_not_called()


class AnnotateResultTests(unittest.TestCase):
    def _mock_env(self) -> Mock:
        env = Mock()
        env.python_version = "3.11"
        env.image = "python:3.11"
        env.dependencies = ()
        env.test_command = ()
        env.timeout_seconds = 900
        env.fingerprint = "fp"
        env.metadata = {}
        return env

    def test_passed_result(self) -> None:
        result = {"passed": True, "stage": "tests"}
        annotated = _annotate_result(result, self._mock_env())
        self.assertEqual(annotated["failure_class"], "none")
        self.assertTrue(annotated["environment_valid"])
        self.assertTrue(annotated["agent_attempted"])

    def test_failed_result(self) -> None:
        result = {"passed": False, "stage": "tests", "detail": "assert error"}
        annotated = _annotate_result(result, self._mock_env())
        self.assertEqual(annotated["failure_class"], "tests")

    def test_environment_invalid(self) -> None:
        result = {"passed": False, "stage": "clone"}
        annotated = _annotate_result(result, self._mock_env())
        self.assertFalse(annotated["environment_valid"])
        self.assertFalse(annotated["agent_attempted"])

    def test_consensus_approved(self) -> None:
        result = {"passed": True, "stage": "tests",
                  "consensus": CandidateConsensus(agreed=True)}
        annotated = _annotate_result(result, self._mock_env())
        self.assertTrue(annotated["consensus_approved"])

    def test_result_code_from_consensus(self) -> None:
        result = {"passed": False, "stage": "governance",
                  "consensus": CandidateConsensus(
                      agreed=False, result_code="CONSENSUS_FAILED")}
        annotated = _annotate_result(result, self._mock_env())
        self.assertEqual(annotated["result_code"], "CONSENSUS_FAILED")


class BuildSwebenchPromptTests(unittest.TestCase):
    def _mock_env(self) -> Mock:
        env = Mock()
        env.python_version = "3.11"
        env.image = "python:3.11"
        env.dependencies = ()
        env.test_command = ()
        env.timeout_seconds = 900
        env.fingerprint = "fp"
        env.metadata = {}
        return env

    def test_prompt_contains_required_sections(self) -> None:
        instance = {
            "repo": "owner/repo",
            "base_commit": "abc123",
            "problem_statement": "Fix the bug",
            "fail_to_pass": ["test_fix"],
            "pass_to_pass": ["test_regression"],
        }
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
            prompt = build_swebench_prompt(
                instance, root, [root / "src" / "main.py"],
                {"src/main.py"}, self._mock_env())
            self.assertIn("REPOSITORY:", prompt)
            self.assertIn("BASE COMMIT:", prompt)
            self.assertIn("ISSUE:", prompt)
            self.assertIn("Fix the bug", prompt)
            self.assertIn("RULES:", prompt)

    def test_prompt_bounded_by_max_chars(self) -> None:
        instance = {
            "repo": "owner/repo",
            "base_commit": "abc",
            "problem_statement": "Fix the bug",
            "fail_to_pass": [],
            "pass_to_pass": [],
        }
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # Create a large file that exceeds max_chars
            (root / "big.py").write_text("x = 1\n" * 10000, encoding="utf-8")
            prompt = build_swebench_prompt(
                instance, root, [root / "big.py"], {"big.py"},
                self._mock_env(), max_chars=200)
            # The large file content should be omitted from prompt
            self.assertNotIn("x = 1", prompt.split("CURRENT STATE:")[-1]
                             if "CURRENT STATE:" in prompt else "")
            # But the issue and other sections should still be present
            self.assertIn("Fix the bug", prompt)

    def test_feedback_section(self) -> None:
        instance = {
            "repo": "owner/repo",
            "base_commit": "abc",
            "problem_statement": "fix",
            "fail_to_pass": [],
            "pass_to_pass": [],
        }
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            prompt = build_swebench_prompt(
                instance, root, [], set(), self._mock_env(),
                feedback="previous attempt failed")
            self.assertIn("EVIDENCE FROM PREVIOUS ATTEMPT:", prompt)
            self.assertIn("previous attempt failed", prompt)

    def test_prompt_authorizes_only_oracle_listed_new_files(self) -> None:
        instance = {
            "repo": "owner/repo",
            "base_commit": "abc",
            "problem_statement": "fix",
            "fail_to_pass": [],
            "pass_to_pass": [],
        }
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "pkg").mkdir()
            (root / "pkg" / "existing.py").write_text("x = 1\n", encoding="utf-8")
            prompt = build_swebench_prompt(
                instance, root, [root / "pkg" / "existing.py"],
                {"pkg/existing.py", "pkg/new_file.py"}, self._mock_env())
            self.assertIn("You may create only these missing files if needed:", prompt)
            self.assertIn("MISSING ALLOWED FILES (create with --- /dev/null / +++ b/<path>):", prompt)
            self.assertIn("pkg/new_file.py", prompt)
            self.assertIn("Do not create any other files.", prompt)
            self.assertIn("Use exact repo-relative paths from ALLOWED FILES in diff headers.", prompt)
            self.assertIn("For a newly created file, use `--- /dev/null` and `+++ b/<exact path>`.", prompt)
            self.assertIn("Existing files:", prompt)
            self.assertIn("New files to create:", prompt)
            self.assertIn("For each file in the diff, the hunk line numbers must match the CURRENT STATE shown.", prompt)


class GenerateCandidateSetTests(unittest.TestCase):
    def test_calls_generate_for_each_request(self) -> None:
        calls = []
        def fake_gen(label, *args):
            calls.append(label)
            return CandidateResult(model=label, patch=None)
        results = _generate_candidate_set(
            [("A", "m1"), ("B", "m2")], generate=fake_gen)
        self.assertEqual(calls, ["A", "B"])
        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
