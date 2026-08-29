"""Fetch real SWE-bench instances from the Hugging Face dataset API."""
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path


def _tests(value: object) -> list[str] | str:
    """Normalize dataset-server's JSON-encoded test lists."""
    if not isinstance(value, str):
        return value  # type: ignore[return-value]
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    return [str(item) for item in parsed] if isinstance(parsed, list) else value


def fetch(dataset: str, split: str, offset: int, length: int) -> list[dict]:
    query = urllib.parse.urlencode({
        "dataset": dataset,
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
            "fail_to_pass": _tests(row["FAIL_TO_PASS"]),
            "pass_to_pass": _tests(row["PASS_TO_PASS"]),
            "version": row["version"],
        })
    return records


def fetch_unique_repos(dataset: str, split: str, offset: int, length: int) -> list[dict]:
    """Fetch a bounded, first-seen sample with no repeated repositories."""
    records: list[dict] = []
    repositories: set[str] = set()
    cursor = offset
    # A dataset can contain many adjacent issues from one repository. Bound the
    # scan so a malformed or narrow split cannot request indefinitely.
    remaining = max(length * 50, 500)
    while len(records) < length and remaining > 0:
        batch_size = min(100, remaining)
        batch = fetch(dataset, split, cursor, batch_size)
        if not batch:
            break
        for record in batch:
            if record["repo"] in repositories:
                continue
            repositories.add(record["repo"])
            records.append(record)
            if len(records) == length:
                break
        cursor += len(batch)
        remaining -= len(batch)
        if len(batch) < batch_size:
            break
    return records


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default="princeton-nlp/SWE-bench_Verified",
                        help="Hugging Face dataset id (default: SWE-bench Verified)")
    parser.add_argument("--split", default="test")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--length", type=int, default=10)
    parser.add_argument("--unique-repos", action="store_true",
                        help="select at most one instance per repository")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    records = (fetch_unique_repos(args.dataset, args.split, args.offset, args.length)
               if args.unique_repos else fetch(
                   args.dataset, args.split, args.offset, args.length))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "name": args.dataset.rsplit("/", 1)[-1].lower().replace("_", "-") + "-real",
        "source": args.dataset,
        "split": args.split,
        "tasks": records,
    }, indent=2), encoding="utf-8")
    print(json.dumps({"source": args.dataset,
                       "split": args.split, "tasks": len(records),
                       "unique_repos": args.unique_repos,
                      "output": str(args.out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
