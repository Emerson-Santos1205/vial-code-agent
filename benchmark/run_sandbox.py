"""Run benchmark test validation in a read-only Docker sandbox."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
# The direct script entry point otherwise places only ``benchmark/`` on the
# import path, making ``benchmark.report`` unavailable to run_benchmark.
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "src"))

from benchmark.run_benchmark import expand_workload  # noqa: E402
from vial_code_agent.patches import PatchApplier  # noqa: E402

IMAGE = "vial-code-agent-test-sandbox:local"


def build_image() -> None:
    subprocess.run(
        ["docker", "build", "-f", str(BASE / "docker" / "sandbox-test.Dockerfile"),
         "-t", IMAGE, str(BASE)], check=True)


def run_task(task: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="vial-sandbox-") as directory:
        root = Path(directory)
        (root / "solution.py").write_text(task["initial"], encoding="utf-8")
        (root / "test_solution.py").write_text(task["tests"], encoding="utf-8")
        PatchApplier(root).apply(task["patch"])
        mount = f"type=bind,src={root},dst=/workspace,readonly"
        result = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--read-only",
             "--tmpfs", "/tmp:rw,noexec,nosuid,nodev", "--mount", mount, IMAGE],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=False,
        )
        return {
            "task_id": task["id"],
            "passed": result.returncode == 0,
            "returncode": result.returncode,
            "detail": (result.stdout + result.stderr).strip()[-1000:],
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload", type=Path,
                        default=Path(__file__).with_name("workload.json"))
    parser.add_argument("--limit", type=int, default=1)
    args = parser.parse_args()
    workload = json.loads(args.workload.read_text(encoding="utf-8"))
    tasks = expand_workload(workload)[:args.limit]
    build_image()
    results = [run_task(task) for task in tasks]
    report = {"sandbox": "docker", "tasks": len(results),
              "passed": sum(row["passed"] for row in results),
              "results": results}
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] == report["tasks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
