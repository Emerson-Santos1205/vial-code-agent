"""Offline, reproducible benchmark for patch application and test validation."""
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
from vial_code_agent.model import OpenCodeProvider
from vial_code_agent.patches import PatchApplier, PatchError


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


def run_task(task: dict, agent_mode: bool = False, model: str = "auto",
             executable: str = "opencode") -> dict:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="vial-code-agent-") as directory:
        root = Path(directory)
        (root / "solution.py").write_text(task["initial"], encoding="utf-8")
        (root / "test_solution.py").write_text(task["tests"], encoding="utf-8")
        try:
            if agent_mode:
                provider = OpenCodeProvider(model, executable=executable, agent="build")
                generated = CodeAgent(provider).generate(
                    task.get("prompt", "solve the task"), root, [root / "solution.py"])
                if generated.patch is None:
                    raise PatchError("agent did not return a patch")
                patch = generated.patch
            else:
                patch = task["patch"]
            PatchApplier(root).apply(patch)
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
        except (PatchError, RuntimeError, subprocess.TimeoutExpired) as error:
            passed = False
            detail = str(error)
    return {
        "task_id": task["id"],
        "category": task.get("category", "fixture"),
        "passed": passed,
        "detail": detail,
        "elapsed_seconds": round(time.monotonic() - started, 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload", type=Path, default=Path(__file__).with_name("workload.json"))
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("results"))
    parser.add_argument("--agent", action="store_true",
                        help="generate patches with the configured coding agent")
    parser.add_argument("--model", default="auto")
    parser.add_argument("--opencode-executable", default="opencode")
    args = parser.parse_args()
    workload = json.loads(args.workload.read_text(encoding="utf-8"))
    tasks = expand_workload(workload)
    rows = [run_task(task, args.agent, args.model, args.opencode_executable)
            for task in tasks]
    passed = sum(row["passed"] for row in rows)
    report = {
        "benchmark": workload["name"],
        "environment": {
            "python": platform.python_version(),
            "mode": "agent" if args.agent else "offline-fixture",
            "model": args.model if args.agent else None,
        },
        "tasks": len(rows),
        "categories": sorted({row["category"] for row in rows}),
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
