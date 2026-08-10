from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .cache import JsonCache, content_digest
from .core import VialCoreReference
from .router import ModelRouter
from .test_runner import run_tests
from .workspace import select_files


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Select context for a VIAL code task")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--vial-root", type=Path)
    parser.add_argument("--include", action="append", default=["*.py"])
    parser.add_argument("--exclude", action="append", default=[".git", ".venv", "__pycache__"])
    parser.add_argument("--task", default="modify source code")
    parser.add_argument("--model", default="auto")
    parser.add_argument("--cache-dir", type=Path, default=Path(".vial-cache"))
    parser.add_argument(
        "--test-command",
        nargs=argparse.REMAINDER,
        help="Command to run after a change; keep this option last",
    )
    parser.add_argument("--test-timeout", type=int, default=120)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    files = select_files(root, args.include, args.exclude)
    route = ModelRouter().route(args.task, args.model)

    print(f"route: {route}")
    if args.vial_root:
        core = VialCoreReference(args.vial_root.resolve())
        print(f"vial_root: {core.root}")
        print(f"vial_core_available: {core.exists()}")
    print(f"selected_files: {len(files)}")
    for path in files:
        print(path.relative_to(root).as_posix())
    if args.test_command:
        cache = JsonCache((root / args.cache_dir).resolve())
        command_digest = hashlib.sha256(
            json.dumps(args.test_command, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        key = content_digest(files) + "-" + command_digest
        cached = cache.get(key)
        if cached is not None:
            print(f"tests: cached ({'passed' if cached['passed'] else 'failed'})")
            return int(not cached["passed"])
        result = run_tests(root, args.test_command, args.test_timeout)
        cache.put(
            key,
            {
                "passed": result.passed,
                "returncode": result.returncode,
                "command": list(result.command),
                "elapsed_seconds": result.elapsed_seconds,
            },
        )
        print(f"tests: {'passed' if result.passed else 'failed'}")
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="")
        return int(not result.passed)
    return 0
