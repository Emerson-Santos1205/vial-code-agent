"""Run VIAL generation against real SWE-bench repository instances."""
from __future__ import annotations

import argparse
import json
import os
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


def _run_test_group(root: Path, tests: list[str], env: dict[str, str]) -> tuple[bool, str]:
    tests = [test for test in tests if "." in test or "::" in test or "/" in test]
    if root.joinpath("tests", "runtests.py").is_file():
        tests = [test for test in tests
                 if test.rsplit(".", 1)[-1].startswith("test")]
        command = [sys.executable, "tests/runtests.py", *tests]
    else:
        command = [sys.executable, "-m", "pytest", "-q", *tests]
    if not tests:
        return True, "no tests selected"
    result = subprocess.run(
        command, cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=900, check=False, env=env)
    return result.returncode == 0, (result.stdout + result.stderr)[-4000:]


def run_instance(instance: dict, model: str, run_tests: bool = False) -> dict:
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
        generated = CodeAgent(provider, runtime=runtime).generate(
            instance["problem_statement"], root, files, runtime=runtime)
        if generated.patch is None:
            return {"id": instance["id"], "passed": False,
                    "stage": "patch_contract", "detail": generated.failure_type,
                    "response": generated.response.text[:2000]}
        try:
            PatchApplier(root).validate(generated.patch,
                                        allowed_paths)
        except PatchError as error:
            return {"id": instance["id"], "passed": False,
                    "stage": "patch_validation", "detail": str(error),
                    "response": generated.response.text[:2000],
                    "patch": generated.patch[:3000]}
        if not run_tests:
            return {"id": instance["id"], "passed": True,
                    "stage": "patch_validated", "attempts": generated.attempts,
                    "tokens": generated.tokens}
        try:
            PatchApplier(root).apply(generated.patch)
        except PatchError as error:
            return {"id": instance["id"], "passed": False,
                    "stage": "patch_apply", "detail": str(error)}
        if (root / "astropy").is_dir():
            legacy_build = subprocess.run(
                 [sys.executable, "-m", "pip", "install", "setuptools<60",
                 "extension-helpers<1.0", "wheel",
                 "--disable-pip-version-check"],
                cwd=root, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=900, check=False)
            if legacy_build.returncode:
                return {"id": instance["id"], "passed": False,
                        "stage": "test_environment",
                        "detail": (legacy_build.stdout + legacy_build.stderr)[-4000:]}
        fixture_ok, fixture_error = _apply_fixture(root, instance.get("test_patch", ""))
        if not fixture_ok:
            return {"id": instance["id"], "passed": False,
                    "stage": "test_fixture", "detail": fixture_error}
        install = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", ".", "--no-deps",
             "--no-build-isolation"],
            cwd=root, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=900, check=False)
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
            dependencies = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(requirements),
                 "--disable-pip-version-check"],
            cwd=root, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=900, check=False, env=test_env)
            if dependencies.returncode:
                return {"id": instance["id"], "passed": False,
                        "stage": "test_environment",
                        "detail": (dependencies.stdout + dependencies.stderr)[-4000:]}
        fail_tests = _as_tests(instance.get("fail_to_pass"))
        pass_tests = _as_tests(instance.get("pass_to_pass"))
        if not fail_tests and not pass_tests:
            return {"id": instance["id"], "passed": False,
                    "stage": "test_selection", "detail": "no benchmark tests"}
        fail_ok, fail_detail = _run_test_group(root, fail_tests, test_env)
        pass_ok, pass_detail = _run_test_group(root, pass_tests, test_env)
        attempts = generated.attempts
        tokens = generated.tokens or 0
        if not (fail_ok and pass_ok):
            feedback = ("The generated patch was applied, but benchmark tests failed. "
                        "Read the exact current source and modify only solution files. "
                        "Do not modify tests.\n\n"
                        f"FAIL_TO_PASS ({'passed' if fail_ok else 'failed'}):\n{fail_detail}\n\n"
                        f"PASS_TO_PASS ({'passed' if pass_ok else 'failed'}):\n{pass_detail}")
            try:
                PatchApplier(root).reverse(generated.patch)
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
            try:
                PatchApplier(root).validate(retry.patch, allowed_paths)
                PatchApplier(root).apply(retry.patch)
            except PatchError as error:
                return {"id": instance["id"], "passed": False,
                        "stage": "test_retry_patch", "detail": str(error)}
            fail_ok, fail_detail = _run_test_group(root, fail_tests, test_env)
            pass_ok, pass_detail = _run_test_group(root, pass_tests, test_env)
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
    args = parser.parse_args()
    workload = json.loads(args.workload.read_text(encoding="utf-8"))
    selected = workload["tasks"][args.offset:args.offset + args.limit]
    results = [run_instance(instance, args.model, args.run_tests) for instance in selected]
    report = {"benchmark": workload.get("name", "swebench"),
              "tasks": len(results), "passed": sum(row["passed"] for row in results),
              "patch_applicable": sum(row.get("stage") in {
                  "patch_validated", "tests"} for row in results),
              "fail_to_pass": sum(row.get("fail_to_pass", False) for row in results),
              "pass_to_pass": sum(row.get("pass_to_pass", False) for row in results),
              "tokens": sum(row.get("tokens", 0) or 0 for row in results),
              "retries": sum(max((row.get("attempts", 1) or 1) - 1, 0)
                             for row in results),
              "results": results}
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] == report["tasks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
