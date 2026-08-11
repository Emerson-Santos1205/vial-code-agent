from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from .agent import CodeAgent
from .cache import JsonCache, content_digest
from .config import load_config
from .core import VialCoreReference
from .model import OpenCodeProvider
from .patches import PatchApplier, PatchError
from .router import ModelRouter
from .command_runner import CommandRunner
from .session import SessionStore
from .test_runner import run_tests
from .telemetry import Telemetry
from .web import serve
from .workspace import select_files


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Select context for a VIAL code task")
    parser.add_argument("command", nargs="?", choices=["review", "fix", "chat", "serve", "run"])
    parser.add_argument("patch_file", nargs="?")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--vial-root", type=Path)
    parser.add_argument("--include", action="append")
    parser.add_argument("--exclude", action="append")
    parser.add_argument("--task", default="modify source code")
    parser.add_argument("--model", default="auto")
    parser.add_argument("--generate", action="store_true", help="generate a patch with opencode")
    parser.add_argument("--apply", action="store_true", help="apply the generated patch")
    parser.add_argument("--yes", action="store_true", help="apply without confirmation")
    parser.add_argument("--keep-on-failure", action="store_true", help="keep changes when tests fail")
    parser.add_argument("--max-context-chars", type=int, default=6_000)
    parser.add_argument("--opencode-executable", default="opencode")
    parser.add_argument("--opencode-agent", default="plan")
    parser.add_argument(
        "--opencode-auto",
        action="store_true",
        help="auto-approve opencode workspace permissions; use only in a trusted workspace",
    )
    parser.add_argument("--cache-dir", type=Path, default=Path(".vial-cache"))
    parser.add_argument(
        "--test-command",
        nargs=argparse.REMAINDER,
        help="Command to run after a change; keep this option last",
    )
    parser.add_argument("--test-timeout", type=int, default=120)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--exec-command")
    parser.add_argument("--unsafe", action="store_true", help="allow non-allowlisted commands")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        config = load_config(root)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    telemetry = Telemetry(
        None if not config.telemetry_file else (root / config.telemetry_file).resolve()
    )
    vial_root = args.vial_root or (root / "vendor" / "vial-core")
    vial = VialCoreReference(vial_root.resolve()) if vial_root.is_dir() else None
    if args.command == "run":
        if not args.exec_command:
            print("error: run requires --exec-command", file=sys.stderr)
            return 2
        try:
            result = CommandRunner(root, unsafe=args.unsafe).run(
                CommandRunner.parse(args.exec_command), args.test_timeout
            )
        except (PermissionError, ValueError, OSError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        return result.returncode
    if args.command == "chat":
        store = SessionStore(root / ".vial-sessions")
        session_id = args.patch_file or store.create()
        print(f"session: {session_id}")
        print("Digite /exit para sair.")
        while True:
            try:
                message = input("you> ")
            except EOFError:
                break
            if message.strip() == "/exit":
                break
            if message.strip():
                store.append(session_id, "user", message)
                print("assistant> Mensagem registrada; conecte um provider para gerar a resposta.")
        return 0
    if args.command == "fix":
        if not args.patch_file:
            print("error: fix requires a task", file=sys.stderr)
            return 2
        args.task = args.patch_file
        args.generate = True
        args.apply = args.apply or args.yes
    if args.command == "review":
        if not args.patch_file:
            print("error: review requires a patch file", file=sys.stderr)
            return 2
        patch_path = Path(args.patch_file)
        if not patch_path.is_absolute():
            patch_path = Path.cwd() / patch_path
        try:
            patch = patch_path.read_text(encoding="utf-8")
            PatchApplier(root).validate(patch)
        except (OSError, PatchError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(f"review: {patch_path}")
        print(f"files: {', '.join(sorted(PatchApplier(root).paths(patch)))}")
        print(patch, end="" if patch.endswith("\n") else "\n")
        telemetry.record("review", files=sorted(PatchApplier(root).paths(patch)))
        return 0
    if args.model == "auto":
        args.model = config.model
    if args.cache_dir == Path(".vial-cache"):
        args.cache_dir = Path(config.cache_dir)
    if args.test_timeout == 120:
        args.test_timeout = config.test_timeout
    if args.max_context_chars == 6_000:
        args.max_context_chars = config.max_context_chars
    if args.opencode_executable == "opencode":
        args.opencode_executable = config.opencode_executable
    if args.opencode_agent == "plan":
        args.opencode_agent = config.opencode_agent
    args.include = args.include or ["*.py"]
    args.exclude = args.exclude or [".git", ".venv", "__pycache__"]
    files = select_files(root, args.include, args.exclude)
    route = ModelRouter().route(args.task, args.model)
    if args.command == "serve":
        print(f"web: http://{args.host}:{args.port}")
        serve(root, args.host, args.port, OpenCodeProvider(route, args.opencode_executable, args.opencode_auto, args.opencode_agent))
        return 0

    print(f"route: {route}")
    if args.vial_root:
        core = VialCoreReference(args.vial_root.resolve())
        print(f"vial_root: {core.root}")
        print(f"vial_core_available: {core.exists()}")
    print(f"selected_files: {len(files)}")
    for path in files:
        print(path.relative_to(root).as_posix())
    if args.yes:
        args.apply = True
    if args.apply and not args.generate:
        print("error: --apply requires --generate", file=sys.stderr)
        return 2
    if args.generate:
        started = time.monotonic()
        try:
            generated = CodeAgent(
                OpenCodeProvider(
                    route, args.opencode_executable, args.opencode_auto, args.opencode_agent
                )
            ).generate(
                args.task, root, files, args.max_context_chars, vial
            )
        except RuntimeError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        telemetry.record(
            "generation",
            model=route,
            returncode=generated.response.returncode,
            duration_seconds=round(time.monotonic() - started, 4),
            input_tokens=generated.response.input_tokens,
            output_tokens=generated.response.output_tokens,
            has_patch=generated.patch is not None,
        )
        if generated.response.returncode != 0:
            print(f"error: model exited with code {generated.response.returncode}", file=sys.stderr)
            if generated.response.stderr:
                print(generated.response.stderr, end="", file=sys.stderr)
            return 1
        if not generated.patch:
            print("error: model did not return a valid unified diff", file=sys.stderr)
            if generated.response.text:
                preview = generated.response.text.strip()[:2000]
                print(f"model response:\n{preview}", file=sys.stderr)
            if generated.response.stderr:
                print(generated.response.stderr, end="", file=sys.stderr)
            print("hint: retry with --opencode-auto in a trusted workspace", file=sys.stderr)
            return 1
        print("patch:")
        print(generated.patch)
        try:
            PatchApplier(root).validate(generated.patch, {path.relative_to(root).as_posix() for path in files})
        except PatchError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        if not args.apply:
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                print("error: use --yes in a non-interactive shell", file=sys.stderr)
                return 2
            if input("Apply this patch? [y/N] ").strip().lower() not in {"y", "yes"}:
                print("patch: not applied")
                return 0
        try:
            if generated.workspace_changed:
                print("patch: already applied by opencode")
            elif vial is not None:
                result = vial.execute_patch(PatchApplier(root), generated.patch, generated.context_id)
                if not result.ok():
                    raise PatchError(result.error or "VIAL tool rejected patch")
            else:
                PatchApplier(root).apply(generated.patch)
        except PatchError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print("patch: applied")
        telemetry.record("patch_applied", model=route, files=sorted(PatchApplier(root).paths(generated.patch)))
        if args.test_command:
            result = run_tests(root, args.test_command, args.test_timeout)
            print(f"tests: {'passed' if result.passed else 'failed'}")
            telemetry.record(
                "tests",
                passed=result.passed,
                returncode=result.returncode,
                duration_seconds=round(result.elapsed_seconds, 4),
            )
            if result.stdout:
                print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, end="")
            if not result.passed and not args.keep_on_failure:
                try:
                    PatchApplier(root).reverse(generated.patch)
                    print("patch: rolled back")
                except PatchError as error:
                    print(f"error: rollback failed: {error}", file=sys.stderr)
                return 1
            return int(not result.passed)
        return 0
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
