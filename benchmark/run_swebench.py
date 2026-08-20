"""Run VIAL generation against real SWE-bench repository instances."""
from __future__ import annotations

import argparse
import json
import os
import shlex
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
    from .environment import EnvironmentResolver, EnvironmentSpec
except ImportError:
    from environment import EnvironmentResolver, EnvironmentSpec


DEFAULT_TEST_IMAGE = "python:3.8-slim"


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
             "--mount", mount, *docker_env,
             docker_image, *command], cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        output = error.output or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(
            command, 124, stdout=output,
            stderr=f"command timed out after {timeout}s")


def _run_test_group(root: Path, tests: list[str], env: dict[str, str],
                    docker_image: str | None = None,
                    dependencies: tuple[str, ...] = (),
                    configured_command: tuple[str, ...] = (),
                    timeout_seconds: int = 900) -> tuple[bool, str]:
    tests = [test for test in tests if "." in test or "::" in test or "/" in test]
    if root.joinpath("tests", "runtests.py").is_file():
        tests = [test for test in tests
                 if test.rsplit(".", 1)[-1].startswith("test")]
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
            "python -m pip install -e . --no-deps --no-build-isolation",
            "python -m pip install 'pytest<8' --disable-pip-version-check",
        ]
        if (root / "astropy").is_dir():
            setup.insert(0, "python -m pip install 'setuptools<60' "
                         "'extension-helpers<1.0' 'setuptools_scm<7' 'numpy<1.22' "
                         "'pyerfa<3' 'PyYAML>=3.13' "
                         "--disable-pip-version-check")
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
    return result.returncode == 0, (result.stdout + result.stderr)[-4000:]


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
        return fail_ok, fail_detail, pass_ok, pass_detail

    def command_for(tests: list[str]) -> str:
        tests = [test for test in tests if "." in test or "::" in test or "/" in test]
        if root.joinpath("tests", "runtests.py").is_file():
            return " ".join(shlex.quote(part) for part in
                             ["python", "tests/runtests.py", *tests])
        return " ".join(shlex.quote(part) for part in
                         ["python", "-m", "pytest", "-q", *tests])

    setup = [
        "python -m pip install -e . --no-deps --no-build-isolation",
        "python -m pip install 'pytest<8' --disable-pip-version-check",
    ]
    if (root / "astropy").is_dir():
        setup.insert(0, "python -m pip install 'setuptools<60' "
                     "'extension-helpers<1.0' 'setuptools_scm<7' 'numpy<1.22' "
                     "'pyerfa<3' 'PyYAML>=3.13' --disable-pip-version-check")
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
    result = _run_command(["sh", "-lc", script], root, env, docker_image,
                          timeout=timeout_seconds)
    output = result.stdout + result.stderr
    fail_detail = output.split("__VIAL_FAIL_END__", 1)[0].split(
        "__VIAL_FAIL_BEGIN__", 1)[-1][-4000:]
    pass_detail = output.split("__VIAL_PASS_BEGIN__", 1)[-1].split(
        "__VIAL_PASS_END__", 1)[0][-4000:]
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
        )
        return "environment" if any(marker in detail for marker in environment_markers) else "tests"
    return "unknown"


def _failure_subclass(stage: str, detail: str = "", result: dict | None = None) -> str:
    """Return a deterministic reason within the top-level failure class."""
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
    return {
        "tasks": total,
        "environment_valid": environment_valid,
        "environment_valid_rate": environment_valid / total if total else 0.0,
        "agent_solved": passed,
        "agent_success_rate": passed / environment_valid if environment_valid else 0.0,
        "end_to_end_success_rate": passed / total if total else 0.0,
    }


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
        runtime.record_consensus(
            decision.id, bool(consensus.get("agreed")),
            float(consensus.get("agreement_ratio", 0.0)),
            models=[str(model) for model in consensus.get("models", ())],
            responses={str(key): str(value)
                       for key, value in (consensus.get("responses") or {}).items()},
            evidence=dict(consensus.get("evidence") or {}),
            note=str(consensus.get("note", "")),
        )
    result = runtime.apply_patch(
        PatchApplier(root), patch, context_id,
        decision=decision, allowed_paths=allowed_paths, reverse=reverse)
    metadata = dict(getattr(result, "metadata", {}) or {})
    return bool(result.ok()), str(getattr(result, "error", "") or ""), metadata


def run_instance(instance: dict, model: str, run_tests: bool = False,
                 docker_image: str | None = None,
                 environment: EnvironmentSpec | None = None,
                 consensus: dict | None = None) -> dict:
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
        provider = DockerOpenCodeProvider(model)
        runtime = VialRuntime(
            VialCoreReference(BASE / "vendor" / "vial-core"),
            root / ".vial-state", persist_state=False)
        runtime.set_workspace_root(root)
        prompt = build_swebench_prompt(
            instance, root, files, allowed_paths,
            environment or EnvironmentSpec(
                python_version="declared-by-image",
                image=docker_image or "host"))
        generated = CodeAgent(provider, runtime=runtime).generate(
            prompt, root, files, runtime=runtime)
        if generated.patch is None:
            return {"id": instance["id"], "passed": False,
                    "stage": "patch_contract", "detail": generated.failure_type,
                    "response": generated.response.text[:2000]}
        generated_patch = generated.patch
        try:
            generated_patch = _validate_candidate(root, generated_patch,
                                                  allowed_paths)
        except PatchError as error:
            return {"id": instance["id"], "passed": False,
                    "stage": "patch_validation", "detail": str(error),
                    "response": generated.response.text[:2000],
                    "patch": generated_patch[:3000]}
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
                    "governance": apply_metadata}
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
    args = parser.parse_args()
    workload = json.loads(args.workload.read_text(encoding="utf-8"))
    consensus_by_id = {}
    if args.consensus_file is not None:
        consensus_by_id = json.loads(
            args.consensus_file.read_text(encoding="utf-8"))
        if not isinstance(consensus_by_id, dict):
            parser.error("--consensus-file must contain a JSON object keyed by task id")
    selected = workload["tasks"][args.offset:args.offset + args.limit]
    resolver = EnvironmentResolver()
    results = []
    for instance in selected:
        environment = resolver.resolve(instance, args.test_image)
        result = run_instance(instance, args.model, args.run_tests,
                              environment.image, environment,
                              consensus_by_id.get(instance.get("id")))
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
        results.append(result)
    for result in results:
        if result.get("passed"):
            result["failure_class"] = "none"
        else:
            result["failure_class"] = _failure_class(
                result.get("stage", ""), result.get("detail", ""))
        result["failure_subclass"] = _failure_subclass(
            result.get("stage", ""), result.get("detail", ""), result)
    failure_breakdown: dict[str, int] = {}
    for result in results:
        key = f"{result['failure_class']}.{result['failure_subclass']}"
        failure_breakdown[key] = failure_breakdown.get(key, 0) + 1
    metrics = success_metrics(results)
    report = {"benchmark": workload.get("name", "swebench"),
              "tasks": len(results), "passed": sum(row["passed"] for row in results),
              "environment_valid": metrics["environment_valid"],
              "environment_valid_rate": metrics["environment_valid_rate"],
              "agent_solved": metrics["agent_solved"],
              "agent_success_rate": metrics["agent_success_rate"],
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
              }}
    args.out.mkdir(parents=True, exist_ok=True)
    output = args.out / f"report-{time.strftime('%Y%m%d-%H%M%S')}.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), **report}, indent=2))
    return 0 if report["passed"] == report["tasks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
