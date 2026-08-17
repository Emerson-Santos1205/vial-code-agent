"""Fetch real SWE-bench Lite instances from the Hugging Face dataset API."""
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path


def fetch(split: str, offset: int, length: int) -> list[dict]:
    query = urllib.parse.urlencode({
        "dataset": "SWE-bench/SWE-bench_Lite",
        "config": "default",
        "split": split,
        "offset": offset,
        "length": length,
    })
    with urllib.request.urlopen(
        f"https://datasets-server.huggingface.co/rows?{query}", timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    records = []
    for item in payload["rows"]:
        row = item["row"]
        records.append({
            "id": row["instance_id"],
            "category": "real_swebench",
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "problem_statement": row["problem_statement"],
            "patch": row["patch"],
            "test_patch": row["test_patch"],
            "fail_to_pass": row["FAIL_TO_PASS"],
            "pass_to_pass": row["PASS_TO_PASS"],
            "version": row["version"],
        })
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="test")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--length", type=int, default=10)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    records = fetch(args.split, args.offset, args.length)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "name": "swebench-lite-real",
        "source": "SWE-bench/SWE-bench_Lite",
        "split": args.split,
        "tasks": records,
    }, indent=2), encoding="utf-8")
    print(json.dumps({"source": "SWE-bench/SWE-bench_Lite",
                      "split": args.split, "tasks": len(records),
                      "output": str(args.out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
