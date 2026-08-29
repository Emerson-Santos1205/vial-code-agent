"""Aggregate reproducible SWE-bench shard reports."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .report import economics_metrics, success_metrics


def aggregate_reports(paths: list[Path]) -> dict[str, object]:
    """Merge shard reports while enforcing one benchmark/model contract."""
    if not paths:
        raise ValueError("at least one report is required")
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    first = reports[0]
    first_execution = first.get("execution") or {}
    contract = (
        first.get("benchmark"),
        first_execution.get("model"),
        tuple(first_execution.get("adapters") or ()),
    )
    rows: dict[tuple[str, str], dict] = {}
    for report in reports:
        execution = report.get("execution") or {}
        current = (
            report.get("benchmark"), execution.get("model"),
            tuple(execution.get("adapters") or ()),
        )
        if current != contract:
            raise ValueError("reports do not share the same benchmark/model/adapters")
        for row in report.get("results", []):
            key = (str(row.get("adapter", "vial")), str(row.get("id", "")))
            if not key[1]:
                raise ValueError("every result must contain an id")
            if key in rows:
                raise ValueError(f"duplicate result: {key[0]}:{key[1]}")
            rows[key] = row

    adapters = list(contract[2])
    results = [rows[key] for key in sorted(rows, key=lambda item: (item[0], item[1]))]
    by_adapter = {
        adapter: {
            "metrics": success_metrics([row for row in results
                                         if row.get("adapter") == adapter]),
            "economics": economics_metrics([row for row in results
                                             if row.get("adapter") == adapter]),
        }
        for adapter in adapters
    }
    metrics = success_metrics(results)
    return {
        "benchmark": contract[0],
        "total": len(results),
        "tasks": len(results),
        "passed": sum(bool(row.get("passed")) for row in results),
        "environment_valid": sum(bool(row.get("environment_valid")) for row in results),
        "environment_invalid": sum(not bool(row.get("environment_valid")) for row in results),
        "agent_solved": metrics["agent_solved"],
        "agent_success_rate": metrics["agent_success_rate"],
        "end_to_end_success_rate": metrics["end_to_end_success_rate"],
        "economics": economics_metrics(results),
        "by_adapter": by_adapter,
        "execution": {
            "model": contract[1], "adapters": adapters,
            "shards": [str(path) for path in paths],
            "source_reports": len(paths),
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = aggregate_reports(args.reports)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "report": str(args.out), "tasks": report["tasks"],
        "passed": report["passed"], "by_adapter": report["by_adapter"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
