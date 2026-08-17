from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from .agent import CodeAgent
from .app import VialTUI
from .cache import JsonCache, content_digest
from .chat import ChatController
from .config import AgentConfig, load_config
from .core import VialCoreReference
from .errors import (ERR_INVALID_CONFIG, ERR_INVALID_USAGE,
                     VialRuntimeError, wrap)
from .model import OpenCodeProvider
from .patches import PatchApplier, PatchError
from .router import (
    ConsensusResult, ModelRouter, RouteDecision, RoutingGraph, VialRouter,
)
from .servers import ServerRegistry
from .command_runner import CommandRunner
from .session import SessionStore
from .test_runner import TestResult, run_tests
from .telemetry import Telemetry
from .vial_runtime import VialRuntime
from .workspace import select_files

MODEL_ALIASES = {
    "fast": "openai/gpt-5.6-luna-fast",
    "reasoning": "openai/gpt-5.6-luna",
}


def _resolve_agent(args_agent: str | None, config_agent: str) -> str:
    """Effective agent: explicit ``--agent`` wins, otherwise the config default."""
    return args_agent or config_agent or "build"


def _build_runtime(root: Path, config: AgentConfig, vial: VialCoreReference | None) -> VialRuntime | None:
    """Construct the composed VIAL runtime when the core checkout exists."""
    if vial is None:
        return None
    price_table = None
    if config.price_table_json:
        try:
            price_table = json.loads(config.price_table_json)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid price_table_json: {error}") from error
    return VialRuntime(
        vial, root / ".vial-state",
        org_id=config.org_id, authority=config.authority, actor=config.actor,
        price_table=price_table, persist_state=config.persist_state,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vial",
        description="VIAL — opencode-style coding agent with governed tools",
    )
    parser.add_argument("project", nargs="?",
                        help="directory to start in (defaults to the workspace root)")
    parser.add_argument("--root", type=Path, help="workspace root (default: cwd)")
    parser.add_argument("--vial-root", type=Path,
                        help="path to the official VIAL core checkout")

    # TUI options (mirror opencode).
    parser.add_argument("-m", "--model", default="auto",
                        help="model in provider/model form; auto = orchestrator routes")
    parser.add_argument("--agent", default=None, choices=["build", "plan"],
                        help="agent profile (build has full access, plan is read-only); defaults to config")
    parser.add_argument("-c", "--continue", dest="continue_session",
                        action="store_true", help="continue the last session")
    parser.add_argument("-s", "--session", help="session id to resume")
    parser.add_argument("--prompt", help="initial prompt to send")
    parser.add_argument("--auto", action="store_true",
                        help="auto-approve workspace permissions (dangerous!)")

    # Non-interactive actions.
    parser.add_argument("--status", action="store_true",
                        help="print the organizational snapshot")
    parser.add_argument("--trace", metavar="DECISION_ID",
                        help="print the audit trail for a Decision")
    parser.add_argument("--decisions", action="store_true",
                        help="list Decisions awaiting consensus or approval")
    parser.add_argument("--approve", metavar="DECISION_ID",
                        help="record human approval for a pending Decision")
    parser.add_argument("--note", default="",
                        help="note attached to an --approve action")
    parser.add_argument("--review", metavar="PATCH_FILE",
                        help="validate and print a patch")
    parser.add_argument("--fix", metavar="TASK",
                        help="generate, apply and verify a change")
    parser.add_argument("--models", action="store_true",
                        help="list models (optionally filtered by --provider)")
    parser.add_argument("--providers", action="store_true",
                        help="list model providers")
    parser.add_argument("--run", metavar="COMMAND",
                        help="run an allowlisted command through the governed tool")

    # Selection, verification and provider controls.
    parser.add_argument("--include", action="append")
    parser.add_argument("--exclude", action="append")
    parser.add_argument("--provider", help="filter model discovery by provider")
    parser.add_argument("--max-context-chars", type=int, default=6_000)
    parser.add_argument("--opencode-executable", default="opencode")
    parser.add_argument("--test-command", nargs=argparse.REMAINDER,
                        help="command to verify a change; keep this option last")
    parser.add_argument("--test-timeout", type=int, default=120)
    parser.add_argument("--no-consensus", action="store_true",
                        help="skip the cross-model consensus gate with an explicit audit note")
    parser.add_argument("--unsafe-direct-apply", action="store_true",
                        help="allow mutation without VIAL Runtime (unsafe compatibility mode)")
    parser.add_argument("--keep-on-failure", action="store_true",
                        help="keep changes when verification fails")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    args = build_parser().parse_args(argv)
    root = (args.root or Path(args.project or Path.cwd())).resolve()
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

    executable = config.opencode_executable if args.opencode_executable == "opencode" else args.opencode_executable
    auto_approve = args.auto or config.auto_approve
    agent = _resolve_agent(args.agent, config.opencode_agent)
    model = MODEL_ALIASES.get(args.model, args.model)
    if model == "auto":
        model = config.model

    if args.models or args.providers:
        try:
            provider = OpenCodeProvider(model, executable, auto_approve, agent)
            output = provider.list_models(args.provider) if args.models else provider.list_providers()
            print(output, end="")
            return 0
        except (OSError, RuntimeError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    try:
        runtime = _build_runtime(root, config, vial)
    except ValueError as error:
        print(f"error: {wrap(error, ERR_INVALID_CONFIG).message}", file=sys.stderr)
        return 2

    if args.status:
        if runtime is None:
            print("vial_core: unavailable", file=sys.stderr)
            return 1
        if args.trace:
            try:
                print(json.dumps(runtime.decision_trace(args.trace), indent=2))
            except KeyError as error:
                print(f"error: {error}", file=sys.stderr)
                return 1
            return 0
        print(json.dumps(runtime.snapshot(), indent=2))
        return 0

    if args.trace:
        print("error: --trace requires --status", file=sys.stderr)
        return 2

    if args.decisions:
        if runtime is None:
            print("vial_core: unavailable", file=sys.stderr)
            return 1
        print(json.dumps(runtime.pending_decisions(), indent=2, ensure_ascii=False))
        return 0

    if args.approve:
        if runtime is None:
            print("vial_core: unavailable", file=sys.stderr)
            return 1
        try:
            record = runtime.approve_decision(
                args.approve, config.authority, note=args.note or "")
        except (KeyError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        runtime.persist()
        print(f"approved: {record.decision_id} by {record.approver}")
        return 0

    if args.review:
        return _run_review(root, args, telemetry)

    if args.fix:
        return _run_fix(root, config, vial, args, telemetry)

    if args.run:
        return _run_governed(root, config, vial, args)

    return _run_tui(root, config, runtime, args)


# --------------------------------------------------------------------------- #
# Non-interactive actions
# --------------------------------------------------------------------------- #
def _run_review(root: Path, args, telemetry) -> int:
    patch_path = Path(args.review)
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


def _run_governed(root: Path, config: AgentConfig, vial: VialCoreReference | None, args) -> int:
    runtime = _build_runtime(root, config, vial)
    if runtime is not None:
        runtime.set_workspace_root(root)
        result = runtime.invoke_tool(
            "TOOL-RUN-BUILD",
            {"command": args.run, "timeout": args.test_timeout},
            objective="run allowlisted command")
        if result.status in {"REJECTED", "UNAVAILABLE"}:
            print(f"error: {result.error or result.status}", file=sys.stderr)
            return 2
        output = result.output or {}
        if output.get("stdout"):
            print(output["stdout"], end="")
        if output.get("stderr"):
            print(output["stderr"], end="", file=sys.stderr)
        return int(output.get("returncode", 1))
    try:
        result = CommandRunner(root, unsafe=False).run(
            CommandRunner.parse(args.run), args.test_timeout
        )
    except (PermissionError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def _run_fix_consensus(root: Path, config: AgentConfig, args, task: str,
                       model: str, executable: str, auto_approve: bool,
                       agent: str) -> ConsensusResult | None:
    """Run cross-model consensus for a mutation-classified ``--fix`` task.

    Only the configured routing pool is used as the candidate set, so a
    consensus is only ever attempted with >=2 independent models. Returns
    ``None`` when the pool has fewer than two candidates (or dispatch fails)
    so ``--fix`` records no consensus and the AuthorizationGate decides.
    """
    registry = ServerRegistry(root)
    pool = list(registry.pool)
    if len(pool) < 2:
        return None
    graph = RoutingGraph(
        registry, default_model=model, executable=executable,
        auto_approve=auto_approve, agent=agent)
    try:
        result, _ = graph.dispatch_consensus(
            task, root, models=pool, require_evidence=True,
            test_command=args.test_command, test_timeout=args.test_timeout)
    except (OSError, RuntimeError):
        return None
    return result


def _run_fix(root: Path, config: AgentConfig, vial: VialCoreReference | None,
             args, telemetry) -> int:
    if args.model != "auto":
        config_model = args.model
    else:
        config_model = config.model
    model = MODEL_ALIASES.get(args.model, args.model)
    if model == "auto":
        model = config_model
    executable = config.opencode_executable if args.opencode_executable == "opencode" else args.opencode_executable
    auto_approve = args.auto or config.auto_approve
    agent = _resolve_agent(args.agent, config.opencode_agent)
    max_chars = args.max_context_chars if args.max_context_chars != 6_000 else config.max_context_chars
    test_timeout = args.test_timeout if args.test_timeout != 120 else config.test_timeout
    includes = args.include or ["*.py"]
    excludes = args.exclude or [".git", ".venv", "__pycache__"]
    files = select_files(root, includes, excludes)

    runtime = None
    try:
        runtime = _build_runtime(root, config, vial)
    except ValueError as error:
        print(f"error: {wrap(error, ERR_INVALID_CONFIG).message}", file=sys.stderr)
        return 2
    if runtime is not None:
        runtime.set_workspace_root(root)
        route = VialRouter(runtime).route(args.fix, model)
    else:
        route = ModelRouter().route(args.fix, model)

    print(f"fix: {args.fix}")
    print(f"route: {route if route is not None else 'deterministic'}")
    print(f"selected_files: {len(files)}")

    started = time.monotonic()
    try:
        provider = OpenCodeProvider(
            route or "auto", executable, auto_approve, agent)
        generated = CodeAgent(provider, runtime=runtime).generate(
            args.fix, root, files, max_chars, vial, runtime=runtime)
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    telemetry.record(
        "fix",
        model=route if route is not None else "deterministic",
        returncode=generated.response.returncode,
        duration_seconds=round(time.monotonic() - started, 4),
        has_patch=generated.patch is not None,
        route=generated.route,
        reused=generated.reused,
        quality=generated.quality,
        attempts=generated.attempts,
        failure_type=generated.failure_type,
        patch_detected=generated.patch is not None,
    )
    if generated.patch is None:
        print(
            f"error: no patch generated ({generated.failure_type or 'UNKNOWN'}) "
            f"after {generated.attempts} attempt(s)", file=sys.stderr)
        if generated.response.text:
            print(f"model response:\n{generated.response.text.strip()[:2000]}", file=sys.stderr)
        return 1
    print("patch:")
    print(generated.patch)
    if not generated.workspace_changed:
        try:
            PatchApplier(root).validate(
                generated.patch, {path.relative_to(root).as_posix() for path in files})
        except PatchError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    try:
        if generated.workspace_changed:
            raise PatchError(
                "provider changed the workspace outside VIAL Runtime; "
                "discard the change and retry")
        if runtime is not None:
            decision = runtime.propose_patch_decision(generated.context_id)
            consensus = None
            if args.no_consensus:
                runtime.approve_decision(
                    decision.id, "operator",
                    note="consensus skipped by operator flag --no-consensus")
                print("consensus: skipped by operator (--no-consensus)")
            else:
                consensus = _run_fix_consensus(
                    root, config, args, args.fix, model, executable,
                    auto_approve, agent)
                if consensus is None:
                    print(
                        "error: no model pool (>=2 models) configured; "
                        "cross-model consensus cannot be verified for this "
                        "mutation", file=sys.stderr)
                    print(
                        "hint: configure a pool or re-run with --no-consensus "
                        "to authorize as operator", file=sys.stderr)
                    return 1
                consensus_kwargs = {
                    "models": list(consensus.responses),
                    "responses": {ref: response.text
                                  for ref, response in consensus.responses.items()},
                }
                if consensus.evidence:
                    consensus_kwargs["evidence"] = consensus.evidence
                runtime.record_consensus(
                    decision.id, consensus.agreed, consensus.agreement_ratio,
                    **consensus_kwargs)
                status = "agreed" if consensus.agreed else "disagreed"
                print(f"consensus: {status} "
                      f"(ratio={consensus.agreement_ratio:.2f}, "
                      f"models={len(consensus.responses)})")
                if not consensus.agreed:
                    print(f"decision_id: {decision.id}", file=sys.stderr)
                    for ref, response in consensus.responses.items():
                        print(f"candidate from {ref}:", file=sys.stderr)
                        print(response.text, file=sys.stderr)
            result = runtime.apply_patch(
                PatchApplier(root), generated.patch, generated.context_id,
                allowed_paths={path.relative_to(root).as_posix() for path in files},
                decision=decision,
            )
            if not result.ok():
                error_code = result.metadata.get("error_code", "")
                if error_code in {"CONSENSUS_REQUIRED", "APPROVAL_REQUIRED"}:
                    print(
                        f"error: {result.error or result.status} "
                        f"({error_code})", file=sys.stderr)
                    print(
                        f"hint: run 'vial --root {root} --decisions' to inspect, "
                        f"then 'vial --root {root} --approve {decision.id}'",
                        file=sys.stderr)
                    return 1
                raise PatchError(result.error or "VIAL tool rejected patch")
        elif args.unsafe_direct_apply:
            PatchApplier(root).apply(generated.patch)
        else:
            raise PatchError(
                "VIAL Runtime is required for mutation; pass --vial-root "
                "or explicitly opt into --unsafe-direct-apply")
    except PatchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print("patch: applied")
    telemetry.record(
        "patch_applied", model=model, route=generated.route, reused=generated.reused,
        files=sorted(PatchApplier(root).paths(generated.patch)),
    )

    if args.test_command:
        return _verify(root, runtime, generated, args, test_timeout, telemetry)
    return 0


def _verify(root, runtime, generated, args, test_timeout, telemetry) -> int:
    if runtime is not None:
        result = runtime.invoke_tool(
            "TOOL-RUN-TEST",
            {"command": args.test_command, "timeout": test_timeout},
            objective="run test command after change",
            context_id=generated.context_id)
        if result.status in {"REJECTED", "UNAVAILABLE"}:
            print(f"tests: not run ({result.error or result.status})")
            return 1
        result = TestResult(
            tuple(args.test_command),
            int(result.output.get("returncode", 1)),
            result.output.get("stdout", ""),
            result.output.get("stderr", ""),
            float(result.output.get("duration", 0.0)),
        )
    else:
        result = run_tests(root, args.test_command, test_timeout)
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
            if runtime is not None:
                runtime.record_rollback(generated.patch)
            print("patch: rolled back")
        except PatchError as error:
            print(f"error: rollback failed: {error}", file=sys.stderr)
        return 1
    return int(not result.passed)


# --------------------------------------------------------------------------- #
# TUI
# --------------------------------------------------------------------------- #
def _run_tui(root: Path, config: AgentConfig, runtime: VialRuntime | None, args) -> int:
    store = SessionStore(root / ".vial-sessions")
    if args.session:
        try:
            store.messages(args.session)
            session_id = args.session
        except (OSError, FileNotFoundError, json.JSONDecodeError):
            print(f"error: unknown session: {args.session}", file=sys.stderr)
            return 2
    elif args.continue_session:
        recent = store.list()
        session_id = recent[0] if recent else store.create()
    else:
        session_id = store.create()

    executable = config.opencode_executable if args.opencode_executable == "opencode" else args.opencode_executable
    auto_approve = args.auto or config.auto_approve
    agent = _resolve_agent(args.agent, config.opencode_agent)
    model = MODEL_ALIASES.get(args.model, args.model)
    if model == "auto":
        model = config.model

    if runtime is not None:
        runtime.set_workspace_root(root)
    registry = ServerRegistry(root)
    provider = OpenCodeProvider(model, executable, auto_approve, agent, config.model_timeout)
    controller = ChatController(
        root, store, session_id, provider, model, executable,
        auto_approve, agent, registry=registry, runtime=runtime,
        model_timeout=config.model_timeout,
    )
    controller.test_timeout = config.test_timeout
    app = VialTUI(controller, prompt=args.prompt or "")
    app.run()
    return 0
