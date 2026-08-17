"""Run VIAL generation against real SWE-bench repository instances."""
from __future__ import annotations

import argparse
import json
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


def run_instance(instance: dict, model: str) -> dict:
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
        files = [root / path for path in changed_paths(instance["patch"])
                 if (root / path).is_file()]
        if not files:
            files = list(root.rglob("*.py"))[:20]
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
                                        {path.relative_to(root).as_posix() for path in files})
            return {"id": instance["id"], "passed": True,
                    "stage": "patch_validated", "attempts": generated.attempts,
                    "tokens": generated.tokens}
        except PatchError as error:
            return {"id": instance["id"], "passed": False,
                    "stage": "patch_validation", "detail": str(error),
                    "response": generated.response.text[:2000],
                    "patch": generated.patch[:3000]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--model", default="openai/gpt-5.6-luna")
    args = parser.parse_args()
    workload = json.loads(args.workload.read_text(encoding="utf-8"))
    results = [run_instance(instance, args.model)
               for instance in workload["tasks"][:args.limit]]
    report = {"benchmark": workload.get("name", "swebench"),
              "tasks": len(results), "passed": sum(row["passed"] for row in results),
              "results": results}
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] == report["tasks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
