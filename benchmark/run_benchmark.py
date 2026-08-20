"""Synthetic unit/regression benchmark for patch and test validation."""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))

from vial_code_agent.agent import CodeAgent
from vial_code_agent.core import VialCoreReference
from vial_code_agent.model import OpenCodeProvider, extract_diff
from vial_code_agent.patches import PatchApplier, PatchError
from vial_code_agent.vial_runtime import VialRuntime


def classify_failure(stage: str, detail: str, applied: bool = False,
                     passed: bool = False) -> tuple[str, str]:
    """Return a stable top-level failure class and reason."""
    if passed or not stage:
        return "none", "none"
    text = detail.lower()
    if stage in {"tests", "test_execution"}:
        return "tests", "timeout" if "timed out" in text else "fail_to_pass"
    if stage in {"patch", "patch_contract"}:
        if "no-op" in text:
            return "patch", "no_op"
        if any(marker in text for marker in (
                "escapes workspace", "outside selected context",
                "symlink", "git metadata")):
            return "patch", "path_violation"
        if "does not apply" in text or "patch failed" in text:
            return "patch", "context_mismatch"
        return "patch", "malformed"
    if stage == "patch_apply":
        return "patch", "apply_failure"
    if stage in {"model", "generation"}:
        return "model", "incomplete_solution"
    if "timed out" in text or "executable not found" in text:
        return "environment", "infrastructure"
    return "environment", "infrastructure"


def expand_workload(workload: dict) -> list[dict]:
    """Expand the compact matrix into reproducible, independently scored tasks."""
    if workload.get("tasks"):
        return workload["tasks"]
    tasks = []
    for family in workload["families"]:
        category = family["category"]
        for index in range(1, family["count"] + 1):
            target = index
            tasks.append({
                "id": f"{category}-{index:02d}",
                "category": category,
                "prompt": f"{family['prompt']} The expected answer is {target}.",
                "initial": "def solve():\n    return 0\n",
                "patch": (
                    "--- a/solution.py\n+++ b/solution.py\n@@ -1,2 +1,2 @@\n"
                    " def solve():\n-    return 0\n+    return " + str(target) + "\n"),
                "tests": (
                    "import unittest\nfrom solution import solve\n\n"
                    "class SolutionTests(unittest.TestCase):\n"
                    "    def test_answer(self):\n"
                    f"        self.assertEqual(solve(), {target})\n\n"
                    "if __name__ == '__main__':\n    unittest.main()\n"),
            })
    return tasks


def summarize(rows: list[dict]) -> dict:
    """Produce comparable quality, efficiency and recovery measurements."""
    passed = sum(row["passed"] for row in rows)
    total = len(rows)
    environment_valid = sum(row.get("failure_class") != "environment" for row in rows)
    agent_success_rate = passed / environment_valid if environment_valid else 0.0
    end_to_end_success_rate = passed / total if total else 0.0
    success = passed / total if total else 0.0
    regression = sum(row["regression"] for row in rows) / total if total else 0.0
    intervention = sum(row["human_intervention"] for row in rows) / total if total else 0.0
    rollback = sum(row["rollback"] for row in rows) / total if total else 0.0
    patch_failures = sum(row["patch_failure"] for row in rows)
    test_failures = sum(row["failure_stage"] in {"tests", "test_execution"}
                        for row in rows)
    score = 100 * (0.5 * success + 0.2 * (1 - regression) +
                   0.15 * (1 - intervention) + 0.15 * (1 - rollback))
    failure_breakdown: dict[str, int] = {}
    for row in rows:
        key = f"{row['failure_class']}.{row['failure_subclass']}"
        failure_breakdown[key] = failure_breakdown.get(key, 0) + 1
    return {
        "tasks": total,
        "passed": passed,
        "success_rate": success,
        "environment_valid": environment_valid,
        "environment_valid_rate": environment_valid / total if total else 0.0,
        "agent_success_rate": agent_success_rate,
        "end_to_end_success_rate": end_to_end_success_rate,
        "regression_rate": regression,
        "human_intervention_rate": intervention,
        "rollback_rate": rollback,
        "patch_failures": patch_failures,
        "patch_failure_rate": patch_failures / total if total else 0.0,
        "test_failures": test_failures,
        "test_failure_rate": test_failures / total if total else 0.0,
        "failure_breakdown": failure_breakdown,
        "retry_rate": (sum(row["attempts"] > 1 for row in rows) /
                       total if total else 0.0),
        "mean_latency_seconds": (sum(row["elapsed_seconds"] for row in rows) /
                                  total if total else 0.0),
        "total_tokens": sum(row["input_tokens"] + row["output_tokens"] for row in rows),
        "tokens_per_success": (sum(row["input_tokens"] + row["output_tokens"]
                                   for row in rows if row["passed"]) /
                               passed if passed else 0.0),
        "vial_agent_score": round(score, 2),
    }


def run_task(task: dict, adapter: str = "fixture", model: str = "auto",
             executable: str = "opencode") -> dict:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="vial-code-agent-") as directory:
        root = Path(directory)
        (root / "solution.py").write_text(task["initial"], encoding="utf-8")
        (root / "test_solution.py").write_text(task["tests"], encoding="utf-8")
        applied = False
        response = None
        rollback = False
        generated = None
        failure_stage = ""
        failure_detail = ""
        try:
            if adapter in {"baseline", "opencode", "vial"}:
                provider = OpenCodeProvider(model, executable=executable, agent="build")
                if adapter == "baseline":
                    response = provider.generate(
                        task.get("prompt", "solve the task"), directory=root,
                        files=[root / "solution.py"])
                    patch = extract_diff(response.text)
                else:
                    runtime = None
                    if adapter == "vial":
                        reference = VialCoreReference(BASE / "vendor" / "vial-core")
                        runtime = VialRuntime(
                            reference, root / ".vial-state", persist_state=False)
                        runtime.set_workspace_root(root)
                    generated = CodeAgent(provider, runtime=runtime).generate(
                        task.get("prompt", "solve the task"), root,
                        [root / "solution.py"], runtime=runtime)
                    patch = generated.patch
                    response = generated.response
                if patch is None:
                    failure_stage = "patch_contract"
                    raise PatchError("agent did not return a patch")
            else:
                patch = task["patch"]
                response = None
            PatchApplier(root).apply(patch)
            applied = True
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "-q", "test_solution.py"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            passed = result.returncode == 0
            detail = (result.stdout + result.stderr).strip()[-1000:]
            if not passed:
                try:
                    PatchApplier(root).reverse(patch)
                    rollback = True
                except PatchError:
                    rollback = False
        except (PatchError, RuntimeError, subprocess.TimeoutExpired) as error:
            passed = False
            detail = str(error)
            failure_detail = detail
            if not failure_stage:
                if isinstance(error, (RuntimeError, subprocess.TimeoutExpired)):
                    failure_stage = "test_execution" if applied else "environment"
                else:
                    failure_stage = "patch" if applied else "patch_contract"
            if applied:
                try:
                    PatchApplier(root).reverse(patch)
                    rollback = True
                except PatchError:
                    rollback = False
    input_tokens = response.input_tokens if response else 0
    output_tokens = response.output_tokens if response else 0
    failure_class, failure_subclass = classify_failure(
        failure_stage, failure_detail or detail, applied, passed)
    return {
        "task_id": task["id"],
        "category": task.get("category", "fixture"),
        "adapter": adapter,
        "passed": passed,
        "regression": applied and not passed,
        "patch_failure": failure_stage in {"patch", "patch_contract"},
        "failure_stage": failure_stage,
        "failure_class": failure_class,
        "failure_subclass": failure_subclass,
        "rollback": rollback,
        "human_intervention": not passed,
        "input_tokens": input_tokens or 0,
        "output_tokens": output_tokens or 0,
        "returncode": response.returncode if response else 0,
        "attempts": generated.attempts if adapter in {"opencode", "vial"} else 1,
        "failure_type": generated.failure_type if adapter in {"opencode", "vial"} else "",
        "detail": detail,
        "elapsed_seconds": round(time.monotonic() - started, 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload", type=Path, default=Path(__file__).with_name("workload.json"))
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("results"))
    parser.add_argument("--agent", action="store_true",
                        help="generate patches with the configured coding agent")
    parser.add_argument("--adapter", choices=["fixture", "baseline", "opencode", "vial"],
                        help="benchmark adapter; --agent is an alias for opencode")
    parser.add_argument("--adapters",
                        help="comma-separated adapters to compare on the same workload")
    parser.add_argument("--model", default="auto")
    parser.add_argument("--opencode-executable", default="opencode")
    args = parser.parse_args()
    workload = json.loads(args.workload.read_text(encoding="utf-8"))
    tasks = expand_workload(workload)
    selected = args.adapters.split(",") if args.adapters else [
        args.adapter or ("opencode" if args.agent else "fixture")]
    invalid = set(selected) - {"fixture", "baseline", "opencode", "vial"}
    if invalid:
        parser.error(f"unknown adapters: {', '.join(sorted(invalid))}")
    rows = [run_task(task, adapter, args.model, args.opencode_executable)
            for adapter in selected for task in tasks]
    passed = sum(row["passed"] for row in rows)
    by_adapter = {}
    for adapter in selected:
        adapter_rows = [row for row in rows if row["adapter"] == adapter]
        adapter_passed = sum(row["passed"] for row in adapter_rows)
        by_adapter[adapter] = summarize(adapter_rows)
        by_adapter[adapter].update({
            "tasks": len(adapter_rows),
        })
    report = {
        "benchmark": workload["name"],
        "benchmark_type": "unit_regression_synthetic",
        "benchmark_scope": (
            "Synthetic fixtures for patch application, rollback, retries and "
            "test validation; not a SWE-bench or coding-agent quality estimate."
        ),
        "environment": {
            "python": platform.python_version(),
            "mode": "comparison" if len(selected) > 1 else selected[0],
            "adapters": selected,
            "model": args.model if any(a in {"opencode", "vial", "baseline"}
                                        for a in selected) else None,
        },
        "tasks": len(rows),
        "categories": sorted({row["category"] for row in rows}),
        "metrics": summarize(rows),
        "by_category": {
            category: summarize([row for row in rows if row["category"] == category])
            for category in sorted({row["category"] for row in rows})
        },
        "by_adapter": by_adapter,
        "passed": passed,
        "quality": passed / len(rows) if rows else 0.0,
        "hypothesis_supported": bool(rows) and passed == len(rows),
        "results": rows,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    output = args.out / f"report-{time.strftime('%Y%m%d-%H%M%S')}.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), **{key: report[key] for key in ("tasks", "passed", "quality", "hypothesis_supported")}}, indent=2))
    return 0 if report["hypothesis_supported"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
