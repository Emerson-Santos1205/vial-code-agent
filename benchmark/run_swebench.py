"""Run VIAL generation against real SWE-bench repository instances."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))

from vial_code_agent.agent import CodeAgent
from vial_code_agent.core import VialCoreReference
from vial_code_agent.docker_provider import DockerOpenCodeProvider
from vial_code_agent.patches import PatchApplier, PatchError
from vial_code_agent.vial_runtime import VialRuntime


DEFAULT_TEST_IMAGE = "python:3.8-slim"


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
        return patch
    except PatchError as original:
        repaired = applier.repair_candidate(patch)
        if repaired is None:
            raise original
        applier.validate(repaired, allowed_paths)
        return repaired


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
                    docker_image: str | None = None) -> tuple[bool, str]:
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
        if (root / "astropy").is_dir():
            # Historical Astropy C extensions require the older NumPy ABI;
            # test requirements may otherwise upgrade it again.
            setup.append("python -m pip install 'numpy<1.22' "
                         "--disable-pip-version-check")
        test_command = " ".join(shlex.quote(part) for part in command)
        command = ["sh", "-lc", " && ".join(setup + [test_command])]
    result = _run_command(command, root, env, docker_image,
                          timeout=1800 if docker_image else 900)
    return result.returncode == 0, (result.stdout + result.stderr)[-4000:]


def _failure_class(stage: str, detail: str = "") -> str:
    """Classify failures without counting infrastructure as model failures."""
    if stage in {"clone", "checkout", "test_environment", "test_fixture",
                 "test_selection"}:
        return "environment"
    if stage in {"patch_contract", "patch_validation", "patch_apply",
                 "test_retry_revert", "test_retry_contract", "test_retry_patch"}:
        return "patch"
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


def run_instance(instance: dict, model: str, run_tests: bool = False,
                 docker_image: str | None = None) -> dict:
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
        provider = DockerOpenCodeProvider(model)
        runtime = VialRuntime(
            VialCoreReference(BASE / "vendor" / "vial-core"),
            root / ".vial-state", persist_state=False)
        runtime.set_workspace_root(root)
        allowed_names = ", ".join(sorted(allowed_paths))
        target_python = "3.9" if docker_image else (
            f"{sys.version_info.major}.{sys.version_info.minor}")
        prompt = (f"{instance['problem_statement']}\n\n"
                  f"You may modify only these solution files: {allowed_names}. "
                  "Do not modify tests or any other file. "
                  f"The test runtime uses Python {target_python}; preserve "
                  "compatibility with it.")
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
        try:
            PatchApplier(root).apply(generated_patch)
        except PatchError as error:
            return {"id": instance["id"], "passed": False,
                    "stage": "patch_apply", "detail": str(error)}
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
        fail_ok, fail_detail = _run_test_group(root, fail_tests, test_env, docker_image)
        pass_ok, pass_detail = _run_test_group(root, pass_tests, test_env, docker_image)
        attempts = generated.attempts
        tokens = generated.tokens or 0
        if not (fail_ok and pass_ok):
            feedback = ("The generated patch was applied, but benchmark tests failed. "
                        "Read the exact current source and modify only solution files. "
                        "Do not modify tests.\n\n"
                        f"FAIL_TO_PASS ({'passed' if fail_ok else 'failed'}):\n{fail_detail}\n\n"
                        f"PASS_TO_PASS ({'passed' if pass_ok else 'failed'}):\n{pass_detail}")
            try:
                PatchApplier(root).reverse(generated_patch)
            except PatchError as error:
                return {"id": instance["id"], "passed": False,
                        "stage": "test_retry_revert", "detail": str(error)}
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
                PatchApplier(root).apply(retry_patch)
            except PatchError as error:
                return {"id": instance["id"], "passed": False,
                        "stage": "test_retry_patch", "detail": str(error)}
            fail_ok, fail_detail = _run_test_group(root, fail_tests, test_env, docker_image)
            pass_ok, pass_detail = _run_test_group(root, pass_tests, test_env, docker_image)
        return {"id": instance["id"], "passed": fail_ok and pass_ok,
                "stage": "tests", "attempts": attempts, "tokens": tokens,
                "fail_to_pass": fail_ok, "pass_to_pass": pass_ok,
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
    args = parser.parse_args()
    workload = json.loads(args.workload.read_text(encoding="utf-8"))
    selected = workload["tasks"][args.offset:args.offset + args.limit]
    results = [run_instance(instance, args.model, args.run_tests, args.test_image)
               for instance in selected]
    for result in results:
        if result.get("passed"):
            result["failure_class"] = "none"
        else:
            result["failure_class"] = _failure_class(
                result.get("stage", ""), result.get("detail", ""))
    report = {"benchmark": workload.get("name", "swebench"),
              "tasks": len(results), "passed": sum(row["passed"] for row in results),
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
              "tokens": sum(row.get("tokens", 0) or 0 for row in results),
              "retries": sum(max((row.get("attempts", 1) or 1) - 1, 0)
                             for row in results),
              "results": results}
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] == report["tasks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
