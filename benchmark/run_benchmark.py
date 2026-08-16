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

from vial_code_agent.patches import PatchApplier, PatchError


def run_task(task: dict) -> dict:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="vial-code-agent-") as directory:
        root = Path(directory)
        (root / "solution.py").write_text(task["initial"], encoding="utf-8")
        (root / "test_solution.py").write_text(task["tests"], encoding="utf-8")
        try:
            PatchApplier(root).apply(task["patch"])
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
        except (PatchError, subprocess.TimeoutExpired) as error:
            passed = False
            detail = str(error)
    return {
        "task_id": task["id"],
        "passed": passed,
        "detail": detail,
        "elapsed_seconds": round(time.monotonic() - started, 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload", type=Path, default=Path(__file__).with_name("workload.json"))
    parser.add_argument("--out", type=Path, default=Path(__file__).with_name("results"))
    args = parser.parse_args()
    workload = json.loads(args.workload.read_text(encoding="utf-8"))
    rows = [run_task(task) for task in workload["tasks"]]
    passed = sum(row["passed"] for row in rows)
    report = {
        "benchmark": workload["name"],
        "environment": {"python": platform.python_version(), "mode": "offline-fixture"},
        "tasks": len(rows),
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
