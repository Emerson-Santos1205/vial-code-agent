"""Deterministic benchmark failure classification.

Classification is reporting metadata only. It never authorizes, rejects, or
executes a VIAL operation.
"""
from __future__ import annotations


def failure_class(stage: str, detail: str = "") -> str:
    if stage in {"clone", "checkout", "test_environment", "test_fixture",
                 "test_selection", "baseline_tests"}:
        return "environment"
    if stage in {"patch_contract", "patch_validation", "patch_apply",
                 "test_retry_revert", "test_retry_contract", "test_retry_patch"}:
        return "patch"
    if stage == "tests":
        markers = (
            "ModuleNotFoundError", "ImportError while loading conftest",
            "could not determine astropy package version", "SyntaxError",
            "failed to create task for container", "executable file not found",
            "command timed out", "subprocess-exited-with-error",
            "error: command '/usr/bin/gcc'", "metadata-generation-failed",
        )
        return "environment" if any(marker in detail for marker in markers) else "tests"
    return "unknown"


def failure_subclass(stage: str, detail: str = "", result: dict | None = None) -> str:
    text = detail.lower()
    if not stage or (result and result.get("passed")):
        return "none"
    if result and result.get("failure_class") == "environment":
        if "python" in text or "version" in text:
            return "python_version"
        if "gcc" in text or "compiler" in text:
            return "compiler"
        if "module" in text or "dependency" in text or "requirements" in text:
            return "dependency"
        return "infrastructure"
    if stage == "tests":
        if "timed out" in text:
            return "timeout"
        if result and not result.get("fail_to_pass", False):
            return "fail_to_pass"
        return "regression" if result and not result.get("pass_to_pass", False) else "wrong_solution"
    if stage in {"patch_contract", "test_retry_contract"}:
        if "no-op" in text:
            return "no_op"
        if "outside selected context" in text or "symlink" in text:
            return "path_violation"
        if "does not apply" in text or "patch failed" in text:
            return "context_mismatch"
        return "malformed"
    if stage in {"patch_validation", "test_retry_patch"}:
        return "context_mismatch" if "does not apply" in text else "apply_failure"
    if stage == "patch_apply":
        return "apply_failure"
    return "infrastructure"


def baseline_is_valid(fail_to_pass: bool, pass_to_pass: bool) -> bool:
    return not fail_to_pass and pass_to_pass


def should_retry_test_failure(fail_detail: str, pass_detail: str) -> bool:
    evidence = f"FAIL_TO_PASS:\n{fail_detail}\n\nPASS_TO_PASS:\n{pass_detail}"
    return failure_class("tests", evidence) != "environment"
