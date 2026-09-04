"""Consolidate SWE-bench shard results into a single summary report.

Reads all ``report-*.json`` files under *results_dir* and produces a markdown
summary suitable for GitHub Actions job summaries or local inspection.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def _load_reports(results_dir: Path) -> list[dict]:
    reports: list[dict] = []
    for report_path in sorted(results_dir.rglob("report-*.json")):
        try:
            with open(report_path, encoding="utf-8") as fh:
                reports.append(json.load(fh))
        except (json.JSONDecodeError, OSError):
            continue
    return reports


def _summarize(reports: list[dict]) -> dict:
    total = len(reports)
    passed = sum(1 for r in reports if r.get("passed"))
    failed = total - passed

    by_adapter: dict[str, dict] = {}
    for report in reports:
        for adapter in report.get("by_adapter", {}):
            if adapter not in by_adapter:
                by_adapter[adapter] = {"tasks": 0, "passed": 0}
            by_adapter[adapter]["tasks"] += 1
            if report.get("passed"):
                by_adapter[adapter]["passed"] += 1

    failure_classes = Counter()
    failure_subclasses = Counter()
    model_usage: dict[str, dict] = {}
    total_tokens = 0
    total_duration = 0.0

    for report in reports:
        fc = report.get("failure_class", "unknown")
        fs = report.get("failure_subclass", "unknown")
        if not report.get("passed"):
            failure_classes[fc] += 1
            failure_subclasses[f"{fc}.{fs}"] += 1

        tokens = report.get("tokens", 0) or 0
        total_tokens += tokens
        total_duration += report.get("duration_seconds", 0) or 0

        for model_name, outcome in report.get("candidate_outcomes", {}).items():
            if model_name not in model_usage:
                model_usage[model_name] = {
                    "tasks": 0, "patches_returned": 0, "patches_valid": 0,
                    "tests_passed": 0, "total_attempts": 0,
                }
            mu = model_usage[model_name]
            mu["tasks"] += 1
            mu["patches_returned"] += int(outcome.get("returned_patch", False))
            mu["patches_valid"] += int(outcome.get("patch_valid", False))
            if outcome.get("tests_passed") is True:
                mu["tests_passed"] += 1
            mu["total_attempts"] += outcome.get("attempts", 0) or 0

    first_attempt_pass = 0
    for r in reports:
        if not r.get("passed"):
            continue
        outcomes = r.get("candidate_outcomes") or r.get("consensus", {}).get("candidate_outcomes", {})
        if not outcomes:
            first_attempt_pass += 1
            continue
        if all(o.get("attempts", 1) <= 1 for o in outcomes.values()):
            first_attempt_pass += 1

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": (passed / total * 100) if total else 0,
        "first_attempt_pass": first_attempt_pass,
        "retry_pass": passed - first_attempt_pass,
        "by_adapter": by_adapter,
        "failure_classes": dict(failure_classes.most_common()),
        "failure_subclasses": dict(failure_subclasses.most_common()),
        "model_usage": model_usage,
        "total_tokens": total_tokens,
        "total_duration_seconds": total_duration,
        "avg_duration_seconds": (total_duration / total) if total else 0,
    }


def _render_markdown(summary: dict) -> str:
    lines: list[str] = []
    lines.append("## SWE-bench Evaluation Summary\n")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Total tasks | {summary['total']} |")
    lines.append(f"| Passed | {summary['passed']} |")
    lines.append(f"| Failed | {summary['failed']} |")
    lines.append(f"| Pass rate | {summary['pass_rate']:.1f}% |")
    lines.append(f"| First-attempt pass | {summary['first_attempt_pass']} |")
    lines.append(f"| Pass after retry | {summary['retry_pass']} |")
    lines.append(f"| Total tokens | {summary['total_tokens']:,} |")
    lines.append(f"| Avg duration/task | {summary['avg_duration_seconds']:.0f}s |")
    lines.append(f"| Total duration | {summary['total_duration_seconds']:.0f}s |")
    lines.append("")

    if summary["by_adapter"]:
        lines.append("### By Adapter\n")
        lines.append("| Adapter | Tasks | Passed | Rate |")
        lines.append("|---------|-------|--------|------|")
        for adapter, data in summary["by_adapter"].items():
            rate = (data["passed"] / data["tasks"] * 100) if data["tasks"] else 0
            lines.append(f"| {adapter} | {data['tasks']} | {data['passed']} | {rate:.1f}% |")
        lines.append("")

    if summary["model_usage"]:
        lines.append("### By Model\n")
        lines.append("| Model | Tasks | Patches | Valid | Tests Pass | Avg Attempts |")
        lines.append("|-------|-------|---------|-------|------------|--------------|")
        for model, data in summary["model_usage"].items():
            avg_att = (data["total_attempts"] / data["tasks"]) if data["tasks"] else 0
            lines.append(
                f"| {model} | {data['tasks']} | {data['patches_returned']} "
                f"| {data['patches_valid']} | {data['tests_passed']} "
                f"| {avg_att:.1f} |"
            )
        lines.append("")

    if summary["failure_classes"]:
        lines.append("### Failure Breakdown\n")
        lines.append("| Class | Count |")
        lines.append("|-------|-------|")
        for cls, count in summary["failure_classes"].items():
            lines.append(f"| {cls} | {count} |")
        lines.append("")

    if summary["failure_subclasses"]:
        lines.append("### Failure Subclasses\n")
        lines.append("| Subclass | Count |")
        lines.append("|----------|-------|")
        for sub, count in summary["failure_subclasses"].items():
            lines.append(f"| {sub} | {count} |")
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize SWE-bench results")
    parser.add_argument(
        "--results-dir", type=Path, default=Path("benchmark/results"),
        help="Directory containing shard result subdirectories")
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Write markdown summary to this file (default: stdout)")
    args = parser.parse_args(argv)

    if not args.results_dir.is_dir():
        print(f"Results directory not found: {args.results_dir}", file=sys.stderr)
        return 1

    reports = _load_reports(args.results_dir)
    if not reports:
        print("No report files found.", file=sys.stderr)
        return 1

    summary = _summarize(reports)
    markdown = _render_markdown(summary)

    if args.output:
        args.output.write_text(markdown, encoding="utf-8")
        print(f"Summary written to {args.output}")
    else:
        print(markdown)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
