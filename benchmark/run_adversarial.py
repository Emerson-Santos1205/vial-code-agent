"""Reproducible adversarial checks for VIAL workspace boundaries."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "src"))

from vial_code_agent.evidence import validate_candidate  # noqa: E402
from vial_code_agent.patches import PatchApplier, PatchError  # noqa: E402
from vial_code_agent.risk import RiskPolicy, classify_task  # noqa: E402


def _expected_rejection(name: str, callback) -> dict:
    try:
        callback()
    except (PatchError, ValueError, PermissionError):
        return {"name": name, "passed": True, "violation": False}
    return {"name": name, "passed": False, "violation": True}


def run() -> dict:
    checks: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="vial-adversarial-") as directory:
        root = Path(directory)
        (root / "source.txt").write_text("old\n", encoding="utf-8")
        checks.append(_expected_rejection(
            "path_traversal",
            lambda: PatchApplier(root).apply(
                "--- a/../outside.txt\n+++ b/../outside.txt\n@@ -1 +1 @@\n-old\n+new\n")))
        checks.append(_expected_rejection(
            "git_metadata",
            lambda: PatchApplier(root).validate(
                "--- a/.git/config\n+++ b/.git/config\n@@ -1 +1 @@\n-old\n+new\n")))
        checks.append(_expected_rejection(
            "allowed_paths",
            lambda: PatchApplier(root).validate(
                "--- a/source.txt\n+++ b/source.txt\n@@ -1 +1 @@\n-old\n+new\n",
                {"other.txt"})))
        checks.append(_expected_rejection(
            "invalid_patch",
            lambda: PatchApplier(root).apply("not a patch")))
        evidence = validate_candidate(
            root,
            "--- a/source.txt\n+++ b/source.txt\n@@ -1 +1 @@\n-old\n+new\n")
        checks.append({"name": "isolated_evidence", "passed": evidence.passed,
                       "violation": not evidence.passed})
        outside = root.parent / f"{root.name}-outside.txt"
        outside.write_text("old\n", encoding="utf-8")
        link = root / "linked.txt"
        try:
            link.symlink_to(outside)
            checks.append(_expected_rejection(
                "symlink_escape",
                lambda: PatchApplier(root).apply(
                    "--- a/linked.txt\n+++ b/linked.txt\n@@ -1 +1 @@\n-old\n+new\n")))
        except (OSError, NotImplementedError):
            checks.append({"name": "symlink_escape", "passed": True,
                           "violation": False, "skipped": True})
        checks.append({"name": "risk_auto_policy",
                       "passed": classify_task("deploy to production") == "critical"
                       and not RiskPolicy("medium").allows_auto("critical"),
                       "violation": False})
    return {
        "benchmark": "vial-code-agent-adversarial",
        "checks": len(checks),
        "passed": sum(check["passed"] for check in checks),
        "security_violations": sum(check["violation"] for check in checks),
        "results": checks,
    }


if __name__ == "__main__":
    report = run()
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["security_violations"] == 0 and
                     report["passed"] == report["checks"] else 1)
