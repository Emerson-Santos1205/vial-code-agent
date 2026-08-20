"""Pure benchmark report metrics."""
from __future__ import annotations


def success_metrics(results: list[dict]) -> dict[str, float | int]:
    """Calculate agent and end-to-end rates without changing execution state."""
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
