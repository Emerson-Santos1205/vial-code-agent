"""Run VIAL generation against real SWE-bench repository instances."""
from __future__ import annotations

import argparse
import json
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))

from vial_code_agent.agent import CodeAgent
from vial_code_agent.core import VialCoreReference
from vial_code_agent.docker_provider import DockerOpenCodeProvider
from vial_code_agent.patches import PatchApplier, PatchError
from vial_code_agent.patch_review import PatchReviewGate
from vial_code_agent.vial_runtime import VialRuntime
try:
    from .report import candidate_metrics
except ImportError:
    from report import candidate_metrics
try:
    from .environment import EnvironmentResolver, EnvironmentSpec
except ImportError:
    from environment import EnvironmentResolver, EnvironmentSpec


DEFAULT_TEST_IMAGE = "python:3.8-slim"


def _workspace_sha256(root: Path) -> str:
    """Fingerprint the checkout state visible to both candidates."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=False,
        capture_output=True, text=True).stdout
    status = subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=root, check=False,
        capture_output=True, text=True).stdout
    diff = subprocess.run(
        ["git", "diff", "--binary"], cwd=root, check=False,
        capture_output=True, text=True).stdout
    return hashlib.sha256((head + status + diff).encode("utf-8")).hexdigest()


def select_test_image(instance: dict, override: str | None = None) -> tuple[str, str]:
    """Backward-compatible view of the resolved environment family."""
    spec = EnvironmentResolver().resolve(instance, override)
    return spec.image, spec.python_version


def build_swebench_prompt(instance: dict, root: Path, files: list[Path],
                          allowed_paths: set[str], environment: EnvironmentSpec,
                          feedback: str = "", max_chars: int = 24000) -> str:
    """Build an explicit, bounded contract for one SWE-bench generation."""
    sections = [
        f"REPOSITORY:\n{instance.get('repo', '')}",
        f"BASE COMMIT:\n{instance.get('base_commit', '')}",
        f"PYTHON:\n{environment.python_version}",
        "ALLOWED FILES:\n" + "\n".join(
            f"- {path}" for path in sorted(allowed_paths)),
        "FILES WERE READ FROM:\n" + "\n".join(
            f"- {path.relative_to(root).as_posix()} ({path})" for path in files),
        f"ISSUE:\n{instance.get('problem_statement', '')}",
        "FAIL_TO_PASS:\n" + "\n".join(_as_tests(instance.get("fail_to_pass"))),
        "PASS_TO_PASS:\n" + "\n".join(_as_tests(instance.get("pass_to_pass"))),
        "RULES:\n"
        "- Do not modify tests.\n"
        "- Do not create files unless explicitly authorized.\n"
        "- Do not change dependencies unless requested.\n"
        f"- Do not use APIs newer than Python {environment.python_version}.\n"
        "- Make the smallest causal change; each hunk must match the current checkout exactly.\n"
        "- If a hunk cannot be anchored to the shown current state, reread the file before answering.\n"
        "- Do not claim tests were executed.\n"
        "- Return a minimal change as one applicable unified diff.",
    ]
    if feedback:
        sections.append(f"EVIDENCE FROM PREVIOUS ATTEMPT:\n{feedback}")
    current = []
    used = sum(len(section) for section in sections)
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        section = f"\nCURRENT STATE: {path.relative_to(root).as_posix()}\n{content}"
        if used + len(section) > max_chars:
            current.append(
                f"CURRENT STATE: {path.relative_to(root).as_posix()}\n"
                "[content omitted from prompt; read the mounted file directly]")
            continue
        current.append(section)
        used += len(section)
    sections.extend(current)
    sections.append("\nReturn only the complete unified diff. Do not include prose.")
    return "\n\n".join(sections)


def changed_paths(patch: str) -> list[str]:
    paths = []
    for line in patch.splitlines():
        if line.startswith("+++ "):
            value = line[4:].split("\t", 1)[0]
            if value != "/dev/null":
                paths.append(value.removeprefix("b/"))
    return sorted(set(paths))


def _as_tests(value: object) -> list[str]:
    if isinstance(value, str):
        items = [line.strip() for line in value.splitlines() if line.strip()]
    else:
        items = [str(item) for item in value or []]
    normalized = []
    for item in items:
        if " (" in item and item.endswith(")"):
            method, owner = item[:-1].split(" (", 1)
            normalized.append(f"{owner}.{method}")
        else:
            normalized.append(item)
    return normalized


def _apply_fixture(root: Path, patch: str) -> tuple[bool, str]:
    """Apply benchmark-owned test fixtures in the disposable clone only.

    Agent patches use ``VialRuntime.apply_patch``; this helper is limited to
    installing the reference test fixture used by the evaluator.
    """
    check = subprocess.run(
        ["git", "apply", "--check", "-"], cwd=root, input=patch,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False)
    if check.returncode:
        return False, check.stderr[-2000:]
    applied = subprocess.run(
        ["git", "apply", "-"], cwd=root, input=patch,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False)
    return applied.returncode == 0, applied.stderr[-2000:]


def _validate_candidate(root: Path, patch: str,
                        allowed_paths: set[str]) -> str:
    """Validate a model patch, repairing only uniquely locatable hunks."""
    applier = PatchApplier(root)
    try:
        applier.validate(patch, allowed_paths)
        candidate = patch
    except PatchError as original:
        repaired = applier.repair_candidate(patch)
        if repaired is None:
            raise original
        applier.validate(repaired, allowed_paths)
        candidate = repaired
    review = PatchReviewGate(root).review(
        candidate, allowed_paths,
        {path for path in allowed_paths if "/test" in path or path.startswith("test")})
    if not review.passed:
        raise PatchError(review.reason)
    return candidate


def _run_command(command: list[str], root: Path, env: dict[str, str],
                 docker_image: str | None = None,
                 timeout: int = 900) -> subprocess.CompletedProcess:
    try:
        if docker_image is None:
            return subprocess.run(command, cwd=root, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout,
                                  check=False, env=env)
        mount = f"type=bind,src={root.resolve().as_posix()},dst=/workspace"
        docker_env = ["-e", "PYTHONPATH=/workspace",
                      "-e", "CFLAGS=-Wno-error=incompatible-pointer-types"]
        return subprocess.run(
            ["docker", "run", "--rm", "--workdir", "/workspace",
             "--mount", mount,
             "--mount", "type=volume,source=vial-swebench-pip-cache," \
                         "destination=/root/.cache/pip",
             *docker_env,
             docker_image, *command], cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        output = error.output or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(
            command, 124, stdout=output,
            stderr=f"command timed out after {timeout}s")


def _normalize_astropy_test_id(test: str) -> str:
    """Let Astropy's custom runner select parametrized tests safely."""
    return re.sub(r"\[[^\]]*\]?$", "", test)


def _run_test_group(root: Path, tests: list[str], env: dict[str, str],
                    docker_image: str | None = None,
                    dependencies: tuple[str, ...] = (),
                    configured_command: tuple[str, ...] = (),
                    timeout_seconds: int = 900) -> tuple[bool, str]:
    tests = [test for test in tests if "." in test or "::" in test or "/" in test]
    tests = [(_normalize_astropy_test_id(test)
              if "[" in test and not test.endswith("]") else test)
             for test in tests]
    if (root.joinpath("tests", "runtests.py").is_file()
            and not (root / "astropy").is_dir()
            and not any("[" in test and not test.endswith("]")
                        for test in tests)):
        tests = [test for test in tests
                 if test.rsplit(".", 1)[-1].startswith("test")]
        tests = [_normalize_astropy_test_id(test) for test in tests]
        command = ["python" if docker_image else sys.executable,
                   "tests/runtests.py", *tests]
    else:
        command = ["python" if docker_image else sys.executable,
                   "-m", "pytest", "-q", *tests]
    if not tests:
        return True, "no tests selected"
    if docker_image:
        requirements = [
            path for path in (
                root / "tests" / "requirements" / "py3.txt",
                root / "requirements" / "test.txt",
            ) if path.is_file()
        ]
        setup = [
            "if [ \"${VIAL_SWEBENCH_ASTROPY:-}\" = 1 ] || "
            "[ \"${VIAL_SWEBENCH_DJANGO:-}\" = 1 ]; then :; else "
            "python -m pip install 'pytest==7.4.4' --disable-pip-version-check; fi",
        ]
        if not (root / "astropy").is_dir():
            setup.insert(0, "python -m pip install -e . --no-deps --no-build-isolation")
        if (root / "astropy").is_dir():
            setup.insert(0, "python -m pip install 'setuptools<60' "
                         "'extension-helpers<1.0' 'setuptools_scm<7' 'numpy<1.22' "
                         "'pyerfa<3' 'PyYAML>=3.13' 'Cython<3' "
                         "'pytest-astropy==0.9.0' 'pytest-astropy-header==0.1.2' "
                         "--disable-pip-version-check")
            # Baseline setup and the candidate copy share the compiled
            # extensions. Rebuilding them for every test group can exceed the
            # executor window on historical Astropy checkouts.
            setup.append("test -n \"$(find astropy -name '*.so' -print -quit)\" || "
                         "python setup.py build_ext --inplace")
        setup.extend(
            "python -m pip install -r "
            + shlex.quote(f"/workspace/{path.relative_to(root).as_posix()}")
            + " --disable-pip-version-check"
            for path in requirements
        )
        if dependencies:
            setup.append("python -m pip install " + " ".join(
                shlex.quote(item) for item in dependencies))
        if (root / "astropy").is_dir():
            # Historical Astropy C extensions require the older NumPy ABI;
            # test requirements may otherwise upgrade it again.
            setup.append("python -m pip install 'numpy<1.22' "
                         "--disable-pip-version-check")
        test_command = (" ".join(shlex.quote(part) for part in configured_command)
                        if configured_command else
                        " ".join(shlex.quote(part) for part in command))
        command = ["sh", "-lc", " && ".join(setup + [test_command])]
    result = _run_command(command, root, env, docker_image,
                          timeout=timeout_seconds)
    detail = (f"STDOUT:\n{result.stdout[-2000:]}\n"
              f"STDERR:\n{result.stderr[-2000:]}\n"
              f"[returncode={result.returncode}]")
    return result.returncode == 0, detail


def _run_test_groups(root: Path, fail_tests: list[str], pass_tests: list[str],
                     env: dict[str, str], docker_image: str | None,
                     dependencies: tuple[str, ...],
                     configured_command: tuple[str, ...],
                     timeout_seconds: int = 900) -> tuple[bool, str, bool, str]:
    """Run both SWE-bench groups in one resolved container environment."""
    if docker_image is None:
        fail_ok, fail_detail = _run_test_group(
            root, fail_tests, env, docker_image, dependencies, configured_command,
            timeout_seconds)
        pass_ok, pass_detail = _run_test_group(
            root, pass_tests, env, docker_image, dependencies, configured_command,
            timeout_seconds)
        if not fail_tests:
            fail_ok, fail_detail = False, "no FAIL_TO_PASS tests selected"
        if not pass_tests:
            pass_ok, pass_detail = True, "no PASS_TO_PASS tests selected"
        return fail_ok, fail_detail, pass_ok, pass_detail

    def command_for(tests: list[str]) -> str:
        tests = [test for test in tests if "." in test or "::" in test or "/" in test]
        tests = [(_normalize_astropy_test_id(test)
                  if "[" in test and not test.endswith("]") else test)
                 for test in tests]
        if not tests:
            return ":"
        if ((root / "tests" / "runtests.py").is_file()
                and not (root / "astropy").is_dir()
                and not any("[" in test and not test.endswith("]")
                            for test in tests)):
            tests = [test for test in tests
                     if test.rsplit(".", 1)[-1].startswith("test")]
            tests = [_normalize_astropy_test_id(test) for test in tests]
            if not tests:
                return ":"
            return " ".join(shlex.quote(part) for part in
                             ["python", "tests/runtests.py", *tests])
        return " ".join(shlex.quote(part) for part in
                         ["python", "-m", "pytest", "-p", "no:warnings", "-q", *tests])

    setup = [
        "if [ \"${VIAL_SWEBENCH_ASTROPY:-}\" = 1 ] || "
        "[ \"${VIAL_SWEBENCH_DJANGO:-}\" = 1 ]; then :; else "
        "python -m pip install 'pytest==7.4.4' --disable-pip-version-check; fi",
    ]
    if not (root / "astropy").is_dir():
        setup.insert(0, "python -m pip install -e . --no-deps --no-build-isolation")
    if (root / "astropy").is_dir():
        setup.insert(0, "python -m pip install 'setuptools<60' "
                     "'extension-helpers<1.0' 'setuptools_scm<7' 'numpy<1.22' "
                     "'pyerfa<3' 'PyYAML>=3.13' 'Cython<3' "
                     "'pytest-astropy==0.9.0' 'pytest-astropy-header==0.1.2' "
                     "--disable-pip-version-check")
        setup.append("test -n \"$(find astropy -name '*.so' -print -quit)\" || "
                     "python setup.py build_ext --inplace")
    requirements = [path for path in (
        root / "tests" / "requirements" / "py3.txt",
        root / "requirements" / "test.txt",
    ) if path.is_file()]
    setup.extend("python -m pip install -r "
                 + shlex.quote(f"/workspace/{path.relative_to(root).as_posix()}")
                 + " --disable-pip-version-check" for path in requirements)
    if dependencies:
        setup.append("python -m pip install " + " ".join(shlex.quote(item)
                                                         for item in dependencies))
    if (root / "astropy").is_dir():
        setup.append("python -m pip install 'numpy<1.22' --disable-pip-version-check")
    script = (" && ".join(setup) + " && "
              f"echo __VIAL_FAIL_BEGIN__ && {command_for(fail_tests)}; "
              f"fail=$?; echo __VIAL_FAIL_END__:$fail; "
              f"echo __VIAL_PASS_BEGIN__ && {command_for(pass_tests)}; "
              f"pass=$?; echo __VIAL_PASS_END__:$pass; exit 0")
    script_path = root / ".vial-test-groups.sh"
    script_path.write_text(script, encoding="utf-8")
    try:
        command = (["sh", "/workspace/.vial-test-groups.sh"]
                   if docker_image else ["sh", "-lc", script])
        result = _run_command(command, root, env, docker_image,
                              timeout=timeout_seconds)
    finally:
        script_path.unlink(missing_ok=True)
    output = result.stdout + result.stderr
    if "__VIAL_FAIL_END__" not in output or "__VIAL_PASS_END__" not in output:
        detail = (f"test group markers missing (returncode={result.returncode})\n"
                  f"STDOUT:\n{result.stdout[-2000:]}\n"
                  f"STDERR:\n{result.stderr[-2000:]}")
        return False, detail, False, detail
    fail_detail = output.split("__VIAL_FAIL_END__", 1)[0].split(
        "__VIAL_FAIL_BEGIN__", 1)[-1][-4000:]
    pass_detail = output.split("__VIAL_PASS_BEGIN__", 1)[-1].split(
        "__VIAL_PASS_END__", 1)[0][-4000:]
    fail_marker = output.split("__VIAL_FAIL_END__", 1)[1].splitlines()[0]
    pass_marker = output.split("__VIAL_PASS_END__", 1)[1].splitlines()[0]
    fail_detail = f"{fail_detail}\n[runner={fail_marker}]"
    pass_detail = f"{pass_detail}\n[runner={pass_marker}]"
    channels = (f"\nSTDOUT:\n{result.stdout[-2000:]}\n"
                f"STDERR:\n{result.stderr[-2000:]}")
    fail_detail += channels
    pass_detail += channels
    fail_ok = "__VIAL_FAIL_END__:0" in output
    pass_ok = "__VIAL_PASS_END__:0" in output
    if result.returncode != 0:
        fail_ok = pass_ok = False
    return fail_ok, fail_detail, pass_ok, pass_detail


def _failure_class(stage: str, detail: str = "") -> str:
    """Classify failures without counting infrastructure as model failures."""
    if stage in {"clone", "checkout", "test_environment", "test_fixture",
                 "test_selection", "baseline_tests"}:
        return "environment"
    if stage in {"patch_contract", "patch_validation", "patch_apply",
                 "test_retry_revert", "test_retry_contract", "test_retry_patch"}:
        return "patch"
    if stage == "governance":
        return "governance"
    if stage == "tests":
        environment_markers = (
            "ModuleNotFoundError", "ImportError while loading conftest",
            "could not determine astropy package version", "SyntaxError",
            "failed to create task for container", "executable file not found",
            "command timed out", "subprocess-exited-with-error",
            "error: command '/usr/bin/gcc'", "metadata-generation-failed",
            "[runner=:4]", "test group markers missing",
        )
        return "environment" if any(marker in detail for marker in environment_markers) else "tests"
    return "unknown"


def _failure_subclass(stage: str, detail: str = "", result: dict | None = None) -> str:
    """Return a deterministic reason within the top-level failure class."""
    text = detail.lower()
    if not stage or (result and result.get("passed")):
        return "none"
    if result and result.get("failure_class") == "environment":
        if "runner=:4" in text or "returncode=4" in text:
            return "test_runner_usage"
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
    if stage == "governance":
        return "authorization"
    if stage in {"clone", "checkout", "test_environment", "test_fixture",
                 "test_selection", "baseline_tests"}:
        if "python" in text or "version" in text:
            return "python_version"
        if "gcc" in text or "compiler" in text:
            return "compiler"
        if "module" in text or "dependency" in text or "requirements" in text:
            return "dependency"
        return "infrastructure"
    return "infrastructure"


def should_retry_test_failure(fail_detail: str, pass_detail: str) -> bool:
    """Retry only test evidence; environment failures stop the instance."""
    evidence = f"FAIL_TO_PASS:\n{fail_detail}\n\nPASS_TO_PASS:\n{pass_detail}"
    return _failure_class("tests", evidence) != "environment"


def baseline_is_valid(fail_to_pass: bool, pass_to_pass: bool) -> bool:
    """The unpatched base must fail only the target behavior."""
    return not fail_to_pass and pass_to_pass


def success_metrics(results: list[dict]) -> dict[str, float | int]:
    """Separate agent performance from failures caused by the environment."""
    total = len(results)
    passed = sum(bool(row.get("passed")) for row in results)
    environment_valid = sum(
        row.get("failure_class") != "environment" for row in results)
    metrics = {
        "tasks": total,
        "environment_valid": environment_valid,
        "environment_valid_rate": environment_valid / total if total else 0.0,
        "agent_solved": passed,
        "agent_success_rate": passed / environment_valid if environment_valid else 0.0,
        "end_to_end_success_rate": passed / total if total else 0.0,
    }
    if any(row.get("candidates") or row.get("candidate_outcomes") or
           (row.get("consensus") or {}).get("candidate_outcomes")
           for row in results):
        metrics.update(candidate_metrics(results))
    return metrics


def _governed_apply(runtime: VialRuntime, root: Path, patch: str,
                    context_id: str, allowed_paths: set[str],
                    consensus: dict | None = None,
                    reverse: bool = False) -> tuple[bool, str, dict]:
    """Apply or reverse an agent patch only through VialRuntime.

    Consensus evidence must be supplied by an external, independent review;
    this helper never fabricates it for a benchmark run.
    """
    decision = runtime.propose_patch_decision(context_id)
    if consensus is not None:
        evidence = dict(consensus.get("evidence") or {})
        candidate_outcomes = dict(consensus.get("candidate_outcomes") or {})
        if candidate_outcomes:
            evidence["candidate_outcomes"] = candidate_outcomes
        evidence["consensus_result"] = {
            "result_code": consensus.get("result_code", ""),
            "candidate_attempts": sum(
                int(outcome.get("attempts", 1) or 1)
                for outcome in candidate_outcomes.values()
                if isinstance(outcome, dict)),
            "candidate_retries": sum(
                int(outcome.get("retries", 0) or 0)
                for outcome in candidate_outcomes.values()
                if isinstance(outcome, dict)),
        }
        runtime.record_consensus(
            decision.id, bool(consensus.get("agreed")),
            float(consensus.get("agreement_ratio", 0.0)),
            models=[str(model) for model in consensus.get("models", ())],
            responses={str(key): str(value)
                       for key, value in (consensus.get("responses") or {}).items()},
            evidence=evidence,
            note=str(consensus.get("note", "")),
        )
    result = runtime.apply_patch(
        PatchApplier(root), patch, context_id,
        decision=decision, allowed_paths=allowed_paths, reverse=reverse)
    metadata = dict(getattr(result, "metadata", {}) or {})
    return bool(result.ok()), str(getattr(result, "error", "") or ""), metadata


def _compare_candidate_results(root: Path, first: str, second: str,
                               allowed_paths: set[str]) -> tuple[bool, str]:
    """Compare independently applicable candidates without touching ``root``."""
    with tempfile.TemporaryDirectory(prefix="vial-consensus-") as directory:
        first_root = Path(directory) / "first"
        second_root = Path(directory) / "second"
        shutil.copytree(root, first_root, ignore=shutil.ignore_patterns(".vial-state"))
        shutil.copytree(root, second_root, ignore=shutil.ignore_patterns(".vial-state"))
        try:
            PatchApplier(first_root).apply(first)
            PatchApplier(second_root).apply(second)
        except PatchError as error:
            return False, f"candidate application failed: {error}"
        paths = set(allowed_paths)
        for patch in (first, second):
            paths.update(PatchApplier(root).paths(patch))
        for relative in sorted(paths):
            first_path = first_root / relative
            second_path = second_root / relative
            first_bytes = first_path.read_bytes() if first_path.is_file() else None
            second_bytes = second_path.read_bytes() if second_path.is_file() else None
            if first_bytes != second_bytes:
                return False, f"candidates produce different results for {relative}"
    return True, "candidates produce the same workspace result"


def _evaluate_candidate_behavior(root: Path, patch: str, instance: dict,
                                 environment: EnvironmentSpec | None,
                                 docker_image: str | None) -> dict[str, object]:
    """Run benchmark tests for one candidate in an isolated copy."""
    with tempfile.TemporaryDirectory(prefix="vial-candidate-test-") as directory:
        candidate_root = Path(directory) / "repo"
        # Consensus candidates only need source and tests. Excluding repository
        # documentation and generated build metadata avoids duplicating hundreds
        # of MB for each historical Astropy candidate.
        shutil.copytree(root, candidate_root,
                        ignore=shutil.ignore_patterns(
                            ".git", ".vial-state", "build", "dist", ".tox",
                            "docs", "examples", "licenses", "__pycache__"))
        try:
            PatchApplier(candidate_root).apply(patch)
            test_patch = instance.get("test_patch", "")
            if test_patch:
                fixture_ok, fixture_error = _apply_fixture(candidate_root, test_patch)
                if not fixture_ok:
                    return {"static_valid": True, "behavioral_passed": False,
                            "detail": f"test fixture failed: {fixture_error}"}
            fail_ok, fail_detail, pass_ok, pass_detail = _run_test_groups(
                candidate_root, _as_tests(instance.get("fail_to_pass")),
                _as_tests(instance.get("pass_to_pass")), os.environ.copy(),
                docker_image, environment.dependencies if environment else (),
                environment.test_command if environment else (),
                environment.timeout_seconds if environment else 900)
        except PatchError as error:
            return {"static_valid": True, "behavioral_passed": False,
                    "detail": f"candidate test application failed: {error}"}
    return {
        "static_valid": True,
        "behavioral_passed": fail_ok and pass_ok,
        "detail": (f"FAIL_TO_PASS: {fail_detail}\nPASS_TO_PASS: {pass_detail}")[-4000:],
    }


def _retry_behavioral_candidate(root: Path, patch: str, instance: dict,
                                environment: EnvironmentSpec | None,
                                docker_image: str | None, model: str,
                                runtime: VialRuntime, files: list[Path],
                                allowed_paths: set[str],
                                behavior: dict[str, object]
                                ) -> tuple[str, dict, int, int]:
    """Give a failing candidate two evidence-driven corrective attempts."""
    current_patch = patch
    current_behavior = dict(behavior)
    total_attempts = total_retries = 0
    for retry_number in range(2):
        feedback = (
            "The candidate patch applied but failed the isolated benchmark tests. "
            "Infer the causal implementation defect from the FAIL_TO_PASS "
            "assertion and expected behavior before editing. Re-read every "
            "target file from the current workspace, preserve existing APIs, "
            "and return only a minimal unified diff. Do not modify tests.\n\n"
            f"BEHAVIORAL RETRY {retry_number + 1}/2:\n" +
            str(current_behavior.get("detail", "")))
        prompt = build_swebench_prompt(
            instance, root, files, allowed_paths,
            environment or EnvironmentSpec(
                python_version="declared-by-image", image=docker_image or "host"),
            feedback=feedback)
        retry = CodeAgent(DockerOpenCodeProvider(model), runtime=runtime).generate(
            prompt, root, files, runtime=runtime)
        attempts = max(int(retry.attempts or 1), 1)
        total_attempts += attempts
        total_retries += 1
        if retry.patch is None:
            current_behavior["detail"] = (
                str(current_behavior.get("detail", "")) +
                "\nBEHAVIORAL RETRY CONTRACT: " +
                str(retry.failure_type or "no patch returned"))[-4000:]
            continue
        try:
            corrected = _validate_candidate(root, retry.patch, allowed_paths)
        except PatchError as error:
            current_behavior["detail"] = (
                str(current_behavior.get("detail", "")) +
                "\nBEHAVIORAL RETRY VALIDATION: " + str(error))[-4000:]
            continue
        corrected_behavior = _evaluate_candidate_behavior(
            root, corrected, instance, environment, docker_image)
        current_patch = corrected
        current_behavior = corrected_behavior
        if corrected_behavior.get("behavioral_passed") is True:
            return current_patch, current_behavior, total_attempts, total_retries
    return current_patch, current_behavior, total_attempts, total_retries


def _candidate_outcome(label: str, model: str, *, returned_patch: bool,
                       patch_valid: bool, tests_passed: bool | None,
                       detail: str = "", attempts: int = 1,
                       retries: int = 0, patch_returns: int | None = None
                       ) -> dict[str, object]:
    """Record the observable pipeline for one independent candidate."""
    if not returned_patch:
        phase = "patch"
        result = f"CANDIDATE_{label}_FAILED"
    elif not patch_valid:
        phase = "static"
        result = f"CANDIDATE_{label}_FAILED"
    elif tests_passed is False:
        phase = "behavioral"
        result = f"CANDIDATE_{label}_FAILED"
    else:
        phase = "result"
        result = f"CANDIDATE_{label}_SUCCEEDED"
    return {
        "candidate_id": label,
        "model": model,
        "pipeline": {
            "patch": "PASS" if returned_patch else "FAIL",
            "static": "PASS" if patch_valid else "FAIL",
            "behavioral": ("PASS" if tests_passed is True else
                            "FAIL" if tests_passed is False else "NOT_RUN"),
            "result": result,
        },
        "returned_patch": returned_patch,
        "patch_returns": (int(returned_patch) if patch_returns is None
                           else patch_returns),
        "attempts": attempts,
        "retries": retries,
        "patch_valid": patch_valid,
        "tests_passed": tests_passed,
        "result_code": result,
        "failure_detail": detail,
    }


def _generate_validated_candidate(label: str, model: str, prompt: str,
                                  root: Path, files: list[Path],
                                  allowed_paths: set[str],
                                  runtime: VialRuntime) -> dict[str, object]:
    """Generate and statically validate one candidate without peer evidence."""
    provider = DockerOpenCodeProvider(model)
    attempts = retries = patch_returns = 0
    diagnostics = []
    generated = None
    candidate_prompt = prompt
    for attempt in range(2):
        generated = CodeAgent(provider, runtime=runtime).generate(
            candidate_prompt, root, files, runtime=runtime)
        generated_attempts = max(int(generated.attempts or 1), 1)
        attempts += generated_attempts
        retries += max(generated_attempts - 1, 0) + int(attempt > 0)
        if generated.patch is None:
            diagnostic = str(generated.failure_type or "no patch returned")
            diagnostics.append(diagnostic)
            candidate_prompt = prompt + (
                "\n\nThe previous response contained no applicable patch. "
                "Retry once from the original task and workspace. Return only "
                "a complete minimal unified diff.\n"
                f"PATCH CONTRACT DIAGNOSTIC: {diagnostic}")
            continue
        patch_returns += 1
        try:
            patch = _validate_candidate(root, generated.patch, allowed_paths)
        except PatchError as error:
            diagnostics.append(str(error))
            candidate_prompt = prompt + (
                "\n\nThe previous patch failed static validation against the "
                "exact workspace. Re-read every target file and verify each "
                "removed line character-for-character before regenerating. "
                "Do not trust stale line numbers, do not repeat an unanchored "
                "hunk, and return only a complete minimal unified diff.\n"
                f"PATCH VALIDATION DIAGNOSTIC: {error}")
            continue
        outcome = _candidate_outcome(
            label, model, returned_patch=True, patch_valid=True,
            tests_passed=None, attempts=attempts, retries=retries,
            patch_returns=patch_returns)
        outcome["response_received"] = bool(
            getattr(getattr(generated, "response", None), "text", ""))
        outcome["prompt_sha256"] = hashlib.sha256(
            candidate_prompt.encode("utf-8")).hexdigest()
        outcome["protocol"] = {
            "output": "unified_diff",
            "validation": "static_then_behavioral",
            "tests": "same_instance_fail_to_pass_pass_to_pass",
        }
        outcome["protocol_sha256"] = hashlib.sha256(json.dumps(
            outcome["protocol"], sort_keys=True).encode("utf-8")).hexdigest()
        outcome["workspace_sha256"] = _workspace_sha256(root)
        return {"model": model, "patch": patch, "generated": generated,
                "outcome": outcome, "behavior": None}
    detail = "; corrective attempt: ".join(diagnostics)
    outcome = _candidate_outcome(
        label, model, returned_patch=patch_returns > 0, patch_valid=False,
        tests_passed=None, detail=detail, attempts=attempts, retries=retries,
        patch_returns=patch_returns)
    outcome["response_received"] = bool(
        getattr(getattr(generated, "response", None), "text", ""))
    outcome["prompt_sha256"] = hashlib.sha256(
        candidate_prompt.encode("utf-8")).hexdigest()
    outcome["protocol"] = {
        "output": "unified_diff",
        "validation": "static_then_behavioral",
        "tests": "same_instance_fail_to_pass_pass_to_pass",
    }
    outcome["protocol_sha256"] = hashlib.sha256(json.dumps(
        outcome["protocol"], sort_keys=True).encode("utf-8")).hexdigest()
    outcome["workspace_sha256"] = _workspace_sha256(root)
    return {"model": model, "patch": None, "generated": generated,
            "outcome": outcome, "behavior": None}


def _generate_candidate_set(requests: list[tuple], generate=None) -> list[dict]:
    """Run every independent generation request without short-circuiting."""
    generate = generate or _generate_validated_candidate
    return [generate(*request) for request in requests]


def _candidate_consensus(root: Path, first: str, second: str,
                         allowed_paths: set[str], models: tuple[str, str],
                         behavioral: dict[str, dict[str, object]] | None = None,
                         run_tests: bool = False) -> dict:
    """Build auditable consensus evidence from two validated patches."""
    equivalent, detail = _compare_candidate_results(root, first, second, allowed_paths)
    behavioral = behavioral or {}
    behavioral_passed = not run_tests or all(
        behavioral.get(model, {}).get("behavioral_passed") is True
        for model in models)
    behavioral_equivalent = run_tests and behavioral_passed
    return {
        "agreed": equivalent and behavioral_passed,
        "agreement_ratio": 1.0 if equivalent else 0.0,
        "models": list(models),
        "responses": {models[0]: first, models[1]: second},
        "evidence": {
            models[0]: behavioral.get(models[0], {
                "static_valid": True, "behavioral_passed": None}),
            models[1]: behavioral.get(models[1], {
                "static_valid": True, "behavioral_passed": None}),
            "comparison": detail,
            "behavioral_equivalent": behavioral_equivalent,
        },
        "candidate_outcomes": {
            models[0]: _candidate_outcome(
                "A", models[0], returned_patch=True, patch_valid=True,
                tests_passed=(behavioral.get(models[0], {}).get(
                    "behavioral_passed") if behavioral else None)),
            models[1]: _candidate_outcome(
                "B", models[1], returned_patch=True, patch_valid=True,
                tests_passed=(behavioral.get(models[1], {}).get(
                    "behavioral_passed") if behavioral else None)),
        },
        "status": "APPROVED" if (
            equivalent and behavioral_passed
        ) else "DISAGREEMENT",
        "result_code": "CONSENSUS_SUCCEEDED" if (
            equivalent and behavioral_passed
        ) else "CONSENSUS_FAILED",
        "note": "independent SWE-bench candidate comparison",
    }


def _candidate_set_consensus(root: Path, candidates: list[dict[str, object]],
                             allowed_paths: set[str], run_tests: bool) -> dict:
    """Evaluate consensus only when two candidates have complete evidence."""
    outcomes = {
        str(candidate["model"]): candidate["outcome"] for candidate in candidates
    }
    qualified = [candidate for candidate in candidates
                 if candidate.get("patch") is not None
                 and bool(candidate["outcome"].get("patch_valid"))
                 and (not run_tests or
                      candidate["outcome"].get("tests_passed") is True)]
    if len(qualified) < 2:
        return {
            "agreed": False,
            "agreement_ratio": 0.0,
            "status": "INSUFFICIENT_CANDIDATES",
            "result_code": "CANDIDATE_SET_INSUFFICIENT",
            "models": [str(candidate["model"]) for candidate in candidates],
            "responses": {},
            "evidence": {
                "qualified_candidates": [str(candidate["model"])
                                           for candidate in qualified],
                "diagnostics": {
                    str(candidate["model"]): candidate["outcome"].get(
                        "failure_detail", "") for candidate in candidates},
            },
            "candidate_outcomes": outcomes,
            "note": "fewer than two candidates have complete passing evidence",
        }
    first, second = qualified[:2]
    consensus = _candidate_consensus(
        root, str(first["patch"]), str(second["patch"]), allowed_paths,
        (str(first["model"]), str(second["model"])),
        behavioral={
            str(candidate["model"]): candidate.get("behavior") or {}
            for candidate in (first, second)
        }, run_tests=run_tests)
    consensus["candidate_outcomes"] = outcomes
    return consensus


def _adjudicated_candidate_consensus(
        root: Path, passing: dict[str, object], adjudicator: dict[str, object],
        original_candidates: list[dict[str, object]],
        allowed_paths: set[str]) -> dict:
    """Mark adjudication only when it supplies a second passing candidate."""
    consensus = _candidate_set_consensus(
        root, [passing, adjudicator], allowed_paths, True)
    consensus["candidate_outcomes"] = {
        **{str(candidate["model"]): candidate["outcome"]
           for candidate in original_candidates},
        str(adjudicator["model"]): adjudicator["outcome"],
    }
    if consensus["agreed"]:
        consensus["status"] = "ADJUDICATED"
        consensus["note"] = (
            "consensus from one original passing candidate and an independent "
            "passing adjudicator")
    return consensus


def _annotate_result(result: dict, environment: EnvironmentSpec) -> dict:
    """Add durable classification fields before a checkpoint is written."""
    result_code = result.get("result_code", "")
    if str(result_code).startswith("CANDIDATE_"):
        result["failure_class"] = "candidate"
        result["failure_subclass"] = str(result_code).lower()
    elif result.get("passed"):
        result["failure_class"] = "none"
        result["failure_subclass"] = "none"
    else:
        result["failure_class"] = _failure_class(
            result.get("stage", ""), result.get("detail", ""))
        result["failure_subclass"] = _failure_subclass(
            result.get("stage", ""), result.get("detail", ""), result)
    environment_invalid_stages = {
        "clone", "checkout", "baseline_tests", "test_environment",
        "test_fixture", "test_selection",
    }
    result["environment_valid"] = result.get("stage") not in environment_invalid_stages
    result["environment_status"] = (
        "VALID" if result["environment_valid"] else "INVALID")
    result["agent_attempted"] = result["stage"] not in environment_invalid_stages
    consensus_result = result.get("consensus") or {}
    result["consensus_approved"] = bool(consensus_result.get("agreed"))
    if consensus_result.get("candidate_outcomes"):
        result["candidate_outcomes"] = consensus_result["candidate_outcomes"]
    result["result_code"] = result.get(
        "result_code", consensus_result.get(
            "result_code", "TASK_SUCCEEDED" if result.get("passed")
            else "TASK_FAILED"))
    result["patch_valid"] = result.get("stage") == "tests"
    result["tests_passed"] = bool(
        result.get("stage") == "tests" and result.get("passed"))
    result["environment"] = {
        "repo": result.get("environment", {}).get("repo", ""),
        "base_commit": result.get("environment", {}).get("base_commit", ""),
        "python_version": environment.python_version,
        "image": environment.image,
        "dependencies": list(environment.dependencies),
        "test_command": list(environment.test_command),
        "timeout_seconds": environment.timeout_seconds,
        "metadata": dict(environment.metadata),
    }
    return result


def run_instance(instance: dict, model: str, run_tests: bool = False,
                 docker_image: str | None = None,
                 environment: EnvironmentSpec | None = None,
                 consensus: dict | None = None,
                 consensus_model: str | None = None,
                 adjudicator_model: str | None = None) -> dict:
    if environment is not None:
        docker_image = environment.image
    with tempfile.TemporaryDirectory(prefix="vial-swebench-") as directory:
        root = Path(directory) / "repo"
        clone = subprocess.run(
            ["git", "clone", "--filter=blob:none", "https://github.com/" +
             instance["repo"] + ".git", str(root)], capture_output=True,
            text=True, encoding="utf-8", errors="replace", check=False)
        if clone.returncode:
            return {"id": instance["id"], "passed": False,
                    "stage": "clone", "detail": clone.stderr[-1000:]}
        checkout = subprocess.run(
            ["git", "checkout", instance["base_commit"]], cwd=root,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=False)
        if checkout.returncode:
            return {"id": instance["id"], "passed": False,
                    "stage": "checkout", "detail": checkout.stderr[-1000:]}
        solution_files = [root / path for path in changed_paths(instance["patch"])
                          if (root / path).is_file()]
        test_files = [root / path for path in changed_paths(instance.get("test_patch", ""))
                      if (root / path).is_file()]
        files = list(dict.fromkeys(solution_files + test_files))
        if not files:
            files = list(root.rglob("*.py"))[:20]
        allowed_paths = {path.relative_to(root).as_posix() for path in solution_files}
        if not allowed_paths:
            allowed_paths = {path.relative_to(root).as_posix() for path in files}
        baseline = None
        if run_tests:
            baseline_fail_tests = _as_tests(instance.get("fail_to_pass"))
            baseline_pass_tests = _as_tests(instance.get("pass_to_pass"))
            baseline_patch = instance.get("test_patch", "")
            baseline_applied, baseline_error = _apply_fixture(root, baseline_patch)
            if not baseline_applied:
                return {"id": instance["id"], "passed": False,
                        "stage": "baseline_tests", "detail": baseline_error}
            baseline_fail, baseline_fail_detail, baseline_pass, baseline_pass_detail = (
                _run_test_groups(root, baseline_fail_tests, baseline_pass_tests,
                                  os.environ.copy(), docker_image,
                                  environment.dependencies if environment else (),
                                  environment.test_command if environment else (),
                                  environment.timeout_seconds if environment else 900))
            PatchApplier(root).reverse(baseline_patch)
            baseline = {
                "fail_to_pass": baseline_fail,
                "pass_to_pass": baseline_pass,
                "expected": baseline_is_valid(baseline_fail, baseline_pass),
                "detail": (f"FAIL_TO_PASS:\n{baseline_fail_detail}\n\n"
                            f"PASS_TO_PASS:\n{baseline_pass_detail}")[-7000:],
            }
            if not baseline["expected"]:
                return {"id": instance["id"], "passed": False,
                        "stage": "baseline_tests", "baseline": baseline,
                        "detail": baseline["detail"]}
        if consensus_model is not None and consensus_model == model:
            return {"id": instance["id"], "passed": False,
                    "stage": "governance",
                    "detail": "consensus model must be independent from primary model"}
        if adjudicator_model is not None and adjudicator_model in {
                model, consensus_model}:
            return {"id": instance["id"], "passed": False,
                    "stage": "governance",
                    "detail": ("adjudicator model must be independent from "
                               "primary and consensus models")}
        if adjudicator_model is not None and consensus_model is None:
            return {"id": instance["id"], "passed": False,
                    "stage": "governance",
                    "detail": "adjudicator model requires a consensus model"}
        runtime = VialRuntime(
            VialCoreReference(BASE / "vendor" / "vial-core"),
            root / ".vial-state", persist_state=False)
        runtime.set_workspace_root(root)
        prompt = build_swebench_prompt(
            instance, root, files, allowed_paths,
            environment or EnvironmentSpec(
                python_version="declared-by-image",
                image=docker_image or "host"))
        if consensus_model is not None:
            review_runtime = VialRuntime(
                VialCoreReference(BASE / "vendor" / "vial-core"),
                root / ".vial-consensus-state", persist_state=False)
            review_runtime.set_workspace_root(root)
            candidates = _generate_candidate_set([
                ("A", model, prompt, root, files, allowed_paths, runtime),
                ("B", consensus_model, prompt, root, files, allowed_paths,
                 review_runtime),
            ])
            primary, secondary = candidates
            candidate_runtimes = {model: runtime, consensus_model: review_runtime}
            for candidate in candidates:
                if candidate["patch"] is None:
                    continue
                if run_tests:
                    behavior = _evaluate_candidate_behavior(
                        root, str(candidate["patch"]), instance, environment,
                        docker_image)
                    candidate["behavior"] = behavior
                    if behavior.get("behavioral_passed") is False:
                        corrected, behavior, retry_attempts, patch_returns = (
                            _retry_behavioral_candidate(
                                root, str(candidate["patch"]), instance,
                                environment, docker_image,
                                str(candidate["model"]),
                                candidate_runtimes[str(candidate["model"])], files,
                                allowed_paths, behavior))
                        candidate["patch"] = corrected
                        candidate["behavior"] = behavior
                        candidate["outcome"]["attempts"] += retry_attempts
                        candidate["outcome"]["retries"] += retry_attempts
                        candidate["outcome"]["patch_returns"] += patch_returns
                    candidate["outcome"]["tests_passed"] = (
                        candidate["behavior"].get("behavioral_passed"))
                    candidate["outcome"]["pipeline"]["behavioral"] = (
                        "PASS" if candidate["outcome"]["tests_passed"] is True
                        else "FAIL")
                    if candidate["outcome"]["tests_passed"] is not True:
                        candidate["outcome"]["result_code"] = (
                            f"CANDIDATE_{candidate['outcome']['candidate_id']}_FAILED")
                        candidate["outcome"]["pipeline"]["result"] = (
                            candidate["outcome"]["result_code"])
                        candidate["outcome"]["failure_detail"] = str(
                            candidate["behavior"].get("detail", ""))
            consensus = _candidate_set_consensus(
                root, candidates, allowed_paths, run_tests)
            passing = [candidate for candidate in candidates
                       if candidate["outcome"].get("patch_valid")
                       and (not run_tests or
                            candidate["outcome"].get("tests_passed") is True)]
            if (adjudicator_model and run_tests and not consensus["agreed"]
                    and passing):
                adjudicator_runtime = VialRuntime(
                    VialCoreReference(BASE / "vendor" / "vial-core"),
                    root / ".vial-adjudicator-state", persist_state=False)
                adjudicator_runtime.set_workspace_root(root)
                diagnostics = {
                    str(candidate["model"]): {
                        "result_code": candidate["outcome"].get("result_code"),
                        "pipeline": candidate["outcome"].get("pipeline"),
                        "behavioral_detail": (candidate.get("behavior") or {}).get(
                            "detail", "")[-1500:],
                    } for candidate in candidates
                }
                adjudicator_prompt = build_swebench_prompt(
                    instance, root, files, allowed_paths,
                    environment or EnvironmentSpec(
                        python_version="declared-by-image",
                        image=docker_image or "host"),
                    feedback=("Generate an independent candidate from the "
                              "original task and workspace. Candidate patches "
                              "are intentionally withheld. Use only this "
                              "diagnostic evidence:\n" +
                              json.dumps(diagnostics, sort_keys=True)))
                adjudicator = _generate_validated_candidate(
                    "ADJUDICATOR", adjudicator_model, adjudicator_prompt, root,
                    files, allowed_paths, adjudicator_runtime)
                if adjudicator["patch"] is not None:
                    adjudicator["behavior"] = _evaluate_candidate_behavior(
                        root, str(adjudicator["patch"]), instance, environment,
                        docker_image)
                    adjudicator["outcome"]["tests_passed"] = (
                        adjudicator["behavior"].get("behavioral_passed"))
                    adjudicator["outcome"]["pipeline"]["behavioral"] = (
                        "PASS" if adjudicator["outcome"]["tests_passed"] is True
                        else "FAIL")
                    if adjudicator["outcome"]["tests_passed"] is not True:
                        adjudicator["outcome"]["result_code"] = (
                            "CANDIDATE_ADJUDICATOR_FAILED")
                        adjudicator["outcome"]["pipeline"]["result"] = (
                            "CANDIDATE_ADJUDICATOR_FAILED")
                        adjudicator["outcome"]["failure_detail"] = str(
                            adjudicator["behavior"].get("detail", ""))
                adjudications = [
                    _adjudicated_candidate_consensus(
                        root, candidate, adjudicator, candidates, allowed_paths)
                    for candidate in passing
                ]
                adjudicated = next(
                    (result for result in adjudications if result["agreed"]),
                    adjudications[-1])
                consensus = adjudicated
                if adjudicated["agreed"]:
                    matched_model = adjudicated["models"][0]
                    matched = next(candidate for candidate in passing
                                   if candidate["model"] == matched_model)
                    passing = [matched, adjudicator]
            if not consensus["agreed"]:
                return {"id": instance["id"], "passed": False,
                        "stage": "governance", "consensus": consensus,
                        "result_code": consensus["result_code"],
                        "candidate_outcomes": consensus["candidate_outcomes"],
                        "detail": consensus["evidence"].get(
                            "comparison", consensus["note"])}
            selected = passing[0]
            generated_patch = str(selected["patch"])
            generated = selected["generated"]
            runtime = candidate_runtimes.get(str(selected["model"]), runtime)
            provider = DockerOpenCodeProvider(str(selected["model"]))
        else:
            primary = _generate_validated_candidate(
                "A", model, prompt, root, files, allowed_paths, runtime)
            if primary["patch"] is None:
                outcome = primary["outcome"]
                return {"id": instance["id"], "passed": False,
                        "stage": "patch_validation" if outcome["returned_patch"]
                        else "patch_contract",
                        "detail": outcome["failure_detail"],
                        "result_code": outcome["result_code"],
                        "candidate_outcomes": {model: outcome}}
            generated_patch = str(primary["patch"])
            generated = primary["generated"]
            provider = DockerOpenCodeProvider(model)
        if not run_tests:
            return {"id": instance["id"], "passed": True,
                    "stage": "patch_validated", "attempts": generated.attempts,
                    "tokens": generated.tokens}
        applied, apply_error, apply_metadata = _governed_apply(
            runtime, root, generated_patch, generated.context_id,
            allowed_paths, consensus=consensus)
        if not applied:
            return {"id": instance["id"], "passed": False,
                        "stage": "governance", "detail": apply_error or
                        "patch application rejected by VialRuntime",
                    "governance": apply_metadata, "consensus": consensus}
        if (root / "astropy").is_dir():
            legacy_build = _run_command(
                 ["python", "-m", "pip", "install", "setuptools<60",
                  "extension-helpers<1.0", "setuptools_scm<7", "wheel",
                  "--disable-pip-version-check"], root, {}, docker_image)
            if legacy_build.returncode:
                return {"id": instance["id"], "passed": False,
                        "stage": "test_environment",
                        "detail": (legacy_build.stdout + legacy_build.stderr)[-4000:]}
        fixture_ok, fixture_error = _apply_fixture(root, instance.get("test_patch", ""))
        if not fixture_ok:
            return {"id": instance["id"], "passed": False,
                    "stage": "test_fixture", "detail": fixture_error}
        install = _run_command(
            ["python", "-m", "pip", "install", "-e", ".", "--no-deps",
             "--no-build-isolation"], root, {}, docker_image)
        test_env = os.environ.copy()
        if install.returncode:
            # Some historical projects require native compilers unavailable on
            # the host. Source-tree tests can still run without installing the
            # package, provided the checkout is first on PYTHONPATH.
            test_env["PYTHONPATH"] = str(root) + os.pathsep + test_env.get("PYTHONPATH", "")
            test_env["PYTHONWARNINGS"] = "ignore"
        requirement_files = [
            root / "tests" / "requirements" / "py3.txt",
            root / "requirements" / "test.txt",
        ]
        for requirements in requirement_files:
            if not requirements.is_file():
                continue
            requirement_path = (f"/workspace/{requirements.relative_to(root).as_posix()}"
                                if docker_image else str(requirements))
            dependencies = _run_command(
                ["python", "-m", "pip", "install", "-r", requirement_path,
                 "--disable-pip-version-check"], root, test_env, docker_image)
            if dependencies.returncode:
                return {"id": instance["id"], "passed": False,
                        "stage": "test_environment",
                        "detail": (dependencies.stdout + dependencies.stderr)[-4000:]}
        if docker_image and not (root / "tests" / "runtests.py").is_file():
            test_runner = _run_command(
                ["python", "-m", "pip", "install", "pytest<8",
                 "--disable-pip-version-check"], root, test_env, docker_image)
            if test_runner.returncode:
                return {"id": instance["id"], "passed": False,
                        "stage": "test_environment",
                        "detail": (test_runner.stdout + test_runner.stderr)[-4000:]}
        fail_tests = _as_tests(instance.get("fail_to_pass"))
        pass_tests = _as_tests(instance.get("pass_to_pass"))
        if not fail_tests and not pass_tests:
            return {"id": instance["id"], "passed": False,
                    "stage": "test_selection", "detail": "no benchmark tests"}
        dependencies = environment.dependencies if environment else ()
        test_command = environment.test_command if environment else ()
        fail_ok, fail_detail, pass_ok, pass_detail = _run_test_groups(
            root, fail_tests, pass_tests, test_env, docker_image,
            dependencies, test_command,
            environment.timeout_seconds if environment else 900)
        attempts = generated.attempts
        tokens = generated.tokens or 0
        if not (fail_ok and pass_ok):
            evidence = f"FAIL_TO_PASS:\n{fail_detail}\n\nPASS_TO_PASS:\n{pass_detail}"
            if not should_retry_test_failure(fail_detail, pass_detail):
                # Infrastructure evidence must not trigger a model retry.
                return {"id": instance["id"], "passed": False,
                        "stage": "tests", "attempts": attempts, "tokens": tokens,
                        "fail_to_pass": fail_ok, "pass_to_pass": pass_ok,
                        "consensus": consensus,
                        "detail": evidence[-7000:]}
            if consensus_model is not None:
                # A replacement patch would no longer have two-candidate
                # approval. Keep the approved patch and block on new evidence.
                return {"id": instance["id"], "passed": False,
                        "stage": "tests", "attempts": attempts,
                        "tokens": tokens, "fail_to_pass": fail_ok,
                        "pass_to_pass": pass_ok, "consensus": consensus,
                        "detail": evidence[-7000:]}
            feedback = ("The generated patch was applied, but benchmark tests failed.\n"
                        f"FAIL_TO_PASS ({'passed' if fail_ok else 'failed'}):\n{fail_detail}\n\n"
                        f"PASS_TO_PASS ({'passed' if pass_ok else 'failed'}):\n{pass_detail}\n\n"
                        f"REJECTED_PATCH:\n{generated_patch[:12000]}")
            feedback = build_swebench_prompt(
                instance, root, files, allowed_paths, environment or EnvironmentSpec(
                    python_version="declared-by-image", image=docker_image or "host"),
                feedback=feedback)
            reverted, revert_error, revert_metadata = _governed_apply(
                runtime, root, generated_patch, generated.context_id,
                allowed_paths, consensus=consensus, reverse=True)
            if not reverted:
                return {"id": instance["id"], "passed": False,
                        "stage": "test_retry_revert", "detail": revert_error or
                        "patch rollback rejected by VialRuntime",
                        "governance": revert_metadata}
            retry = CodeAgent(provider, runtime=runtime).generate(
                feedback, root, files, runtime=runtime)
            attempts += retry.attempts
            tokens += retry.tokens or 0
            if retry.patch is None:
                return {"id": instance["id"], "passed": False,
                        "stage": "test_retry_contract", "detail": retry.failure_type}
            retry_patch = retry.patch
            try:
                retry_patch = _validate_candidate(root, retry_patch,
                                                  allowed_paths)
            except PatchError as error:
                return {"id": instance["id"], "passed": False,
                        "stage": "test_retry_patch", "detail": str(error)}
            applied, apply_error, apply_metadata = _governed_apply(
                runtime, root, retry_patch, retry.context_id,
                allowed_paths, consensus=consensus)
            if not applied:
                return {"id": instance["id"], "passed": False,
                        "stage": "test_retry_patch", "detail": apply_error or
                        "retry patch rejected by VialRuntime",
                        "governance": apply_metadata}
            fail_ok, fail_detail, pass_ok, pass_detail = _run_test_groups(
                root, fail_tests, pass_tests, test_env, docker_image,
                dependencies, test_command,
                environment.timeout_seconds if environment else 900)
        return {"id": instance["id"], "passed": fail_ok and pass_ok,
                "stage": "tests", "attempts": attempts, "tokens": tokens,
                "fail_to_pass": fail_ok, "pass_to_pass": pass_ok,
                "baseline": baseline,
                "consensus": consensus,
                "detail": (f"FAIL_TO_PASS:\n{fail_detail}\n\n"
                            f"PASS_TO_PASS:\n{pass_detail}")[-7000:]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--model", default="openai/gpt-5.6-luna")
    parser.add_argument("--run-tests", action="store_true")
    parser.add_argument("--test-image", default=None,
                        help="run install and tests in a Docker Python image")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).with_name("results"),
                        help="directory for the reproducible JSON report")
    parser.add_argument("--consensus-file", type=Path, default=None,
                        help="JSON map of task id to independent consensus evidence")
    parser.add_argument("--consensus-model", default=None,
                        help="independent second model used to validate each patch")
    parser.add_argument("--adjudicator-model", default=None,
                        help="optional independent adjudicator for divergent candidates")
    args = parser.parse_args()
    if not args.run_tests:
        parser.error("--run-tests is required: SWE-bench needs environment and baseline validation before the agent")
    workload = json.loads(args.workload.read_text(encoding="utf-8"))
    consensus_by_id = {}
    if args.consensus_file is not None:
        consensus_by_id = json.loads(
            args.consensus_file.read_text(encoding="utf-8"))
        if not isinstance(consensus_by_id, dict):
            parser.error("--consensus-file must contain a JSON object keyed by task id")
    selected = workload["tasks"][args.offset:args.offset + args.limit]
    resolver = EnvironmentResolver()
    args.out.mkdir(parents=True, exist_ok=True)
    checkpoint = args.out / "checkpoint.jsonl"
    completed: dict[int, dict] = {}
    if checkpoint.is_file():
        for line in checkpoint.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                completed[int(entry["index"])] = dict(entry["result"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    results = [completed[index] for index in range(args.offset, args.offset + args.limit)
               if index in completed]
    for index, instance in enumerate(selected, start=args.offset):
        if index in completed:
            continue
        environment = resolver.resolve(instance, args.test_image)
        started = time.monotonic()
        result = run_instance(instance, args.model, args.run_tests,
                              environment.image, environment,
                              consensus_by_id.get(instance.get("id")),
                              args.consensus_model, args.adjudicator_model)
        result["environment"] = {
            "repo": instance.get("repo", ""),
            "base_commit": instance.get("base_commit", ""),
            "python_version": environment.python_version,
            "image": environment.image,
            "dependencies": list(environment.dependencies),
            "test_command": list(environment.test_command),
            "timeout_seconds": environment.timeout_seconds,
            "metadata": dict(environment.metadata),
        }
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        result = _annotate_result(result, environment)
        results.append(result)
        with checkpoint.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"index": index, "result": result}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    failure_breakdown: dict[str, int] = {}
    for result in results:
        key = f"{result['failure_class']}.{result['failure_subclass']}"
        failure_breakdown[key] = failure_breakdown.get(key, 0) + 1
    metrics = success_metrics(results)
    report = {"benchmark": workload.get("name", "swebench"),
              "total": len(results), "tasks": len(results),
              "passed": sum(row["passed"] for row in results),
              "environment_valid": sum(row["environment_valid"] for row in results),
              "environment_invalid": sum(not row["environment_valid"] for row in results),
              "environment_valid_rate": metrics["environment_valid_rate"],
              "agent_attempted": sum(row["agent_attempted"] for row in results),
              "consensus_approved": sum(row["consensus_approved"] for row in results),
              "patch_valid": sum(row["patch_valid"] for row in results),
              "tests_passed": sum(row["tests_passed"] for row in results),
              "agent_solved": metrics["agent_solved"],
               "agent_success_rate": metrics["agent_success_rate"],
               "candidate_attempts": metrics.get("candidate_attempts", 0),
               "candidate_retries": metrics.get("candidate_retries", 0),
               "candidate_returned_patch": metrics.get(
                   "candidate_returned_patch", 0),
               "valid_patch": metrics.get("valid_patch", 0),
               "tests_passed_by_candidate": metrics.get("tests_passed", 0),
               "static_evidence": metrics.get("static_evidence", 0),
               "behavioral_evidence": metrics.get("behavioral_evidence", 0),
               "complete_evidence": metrics.get("complete_evidence", 0),
               "reliable_candidates": metrics.get("reliable_candidates", 0),
               "both_valid": metrics.get("both_valid", 0),
               "agreement": metrics.get("agreement", 0),
                "candidate_a_patch_valid": metrics.get(
                    "candidate_a_patch_valid", 0),
                "candidate_b_patch_valid": metrics.get(
                    "candidate_b_patch_valid", 0),
                "candidate_a_valid": metrics.get("candidate_a_valid", 0),
                "candidate_b_valid": metrics.get("candidate_b_valid", 0),
                "consensus_success": metrics.get("consensus_success", 0),
                "candidate_failure_breakdown": metrics.get(
                    "candidate_failure_breakdown", {"A": {}, "B": {}}),
                "diagnostic_table": metrics.get("diagnostic_table", {}),
                "hash_parity": metrics.get("hash_parity", {}),
                "protocol_parity_tasks": metrics.get(
                    "protocol_parity_tasks", 0),
                "protocol_parity": metrics.get("protocol_parity", 0),
               "candidate_completion_rate": metrics.get(
                   "candidate_completion_rate", 0.0),
               "candidate_patch_validity": metrics.get(
                   "candidate_patch_validity", 0.0),
               "candidate_behavioral_success": metrics.get(
                   "candidate_behavioral_success", 0.0),
               "swebench_evidence_rate": metrics.get(
                   "swebench_evidence_rate", 0.0),
               "candidate_behavioral_evidence_rate": metrics.get(
                   "candidate_behavioral_evidence_rate", 0.0),
               "candidate_reliability_rate": metrics.get(
                   "candidate_reliability_rate", 0.0),
                "candidate_agreement": metrics.get(
                    "candidate_agreement", 0.0),
                "candidate_a_success_rate": metrics.get(
                    "candidate_a_success_rate", 0.0),
                "candidate_b_success_rate": metrics.get(
                    "candidate_b_success_rate", 0.0),
                "candidate_a_patch_validity_rate": metrics.get(
                    "candidate_a_patch_validity_rate", 0.0),
                "candidate_b_patch_validity_rate": metrics.get(
                    "candidate_b_patch_validity_rate", 0.0),
                "both_valid_rate": metrics.get("both_valid_rate", 0.0),
                "consensus_success_rate": metrics.get(
                    "consensus_success_rate", 0.0),
                "protocol_parity_rate": metrics.get(
                    "protocol_parity_rate", 0.0),
               "candidate_failure_results": sum(
                   1 for row in results
                   for outcome in (row.get("candidate_outcomes") or {}).values()
                   if str(outcome.get("result_code", "")).startswith("CANDIDATE_")
                   and str(outcome.get("result_code", "")).endswith("_FAILED")),
               "consensus_failure_results": sum(
                   row.get("result_code") == "CONSENSUS_FAILED"
                   for row in results),
               "end_to_end_success": metrics["agent_solved"],
              "end_to_end_success_rate": metrics["end_to_end_success_rate"],
              "patch_applicable": sum(row.get("stage") in {
                  "patch_validated", "tests"} for row in results),
              "fail_to_pass": sum(row.get("fail_to_pass", False) for row in results),
              "pass_to_pass": sum(row.get("pass_to_pass", False) for row in results),
              "patch_failures": sum(row.get("failure_class") == "patch"
                                     for row in results),
              "environment_failures": sum(row.get("failure_class") == "environment"
                                          for row in results),
               "test_failures": sum(row.get("failure_class") == "tests"
                                    for row in results),
               "governance_failures": sum(row.get("failure_class") == "governance"
                                           for row in results),
              "baseline_checked": sum("baseline" in row for row in results),
              "baseline_failures": sum(row.get("stage") == "baseline_tests"
                                       for row in results),
              "failure_breakdown": failure_breakdown,
              "tokens": sum(row.get("tokens", 0) or 0 for row in results),
              "retries": sum(max((row.get("attempts", 1) or 1) - 1, 0)
                             for row in results),
              "results": results,
              "execution": {
                  "model": args.model,
                  "run_tests": args.run_tests,
                  "offset": args.offset,
                  "limit": args.limit,
                  "test_image_override": args.test_image,
                  "consensus_file": str(args.consensus_file)
                  if args.consensus_file is not None else None,
                   "consensus_model": args.consensus_model,
                   "adjudicator_model": args.adjudicator_model,
              }}
    output = args.out / f"report-{time.strftime('%Y%m%d-%H%M%S')}.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), **report}, indent=2))
    return 0 if report["passed"] == report["tasks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
