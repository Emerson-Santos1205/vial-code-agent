from __future__ import annotations

from dataclasses import asdict
import argparse
import json
from pathlib import Path
import sys

from .core import (
    build_changelog,
    check_repository,
    release_project,
    rollback_project,
    scan_repository,
    sync_core_submodule,
)
from .domain import release_tag, validate_semver


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=Path.cwd())

    parser = argparse.ArgumentParser(prog="release-orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", parents=[common])
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=_scan)

    changelog = subparsers.add_parser("changelog", parents=[common])
    changelog.add_argument("since_tag")
    changelog.add_argument("--force", action="store_true")
    changelog.add_argument("--json", action="store_true")
    changelog.set_defaults(func=_changelog)

    check = subparsers.add_parser("check", parents=[common])
    check.add_argument("--allow-dirty", action="store_true")
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=_check)

    sync_core = subparsers.add_parser("sync-core", parents=[common])
    sync_core.add_argument("--update", action="store_true", help="Atualiza o submódulo VIAL Core se passar nos testes")
    sync_core.add_argument("--json", action="store_true")
    sync_core.set_defaults(func=_sync_core)

    release = subparsers.add_parser("release", parents=[common])
    release.add_argument("version")
    release.add_argument("--confirm", action="store_true")
    release.add_argument("--dry-run", action="store_true")
    release.set_defaults(func=_release)

    rollback = subparsers.add_parser("rollback", parents=[common])
    rollback.add_argument("version")
    rollback.add_argument("--confirm", action="store_true")
    rollback.add_argument("--dry-run", action="store_true")
    rollback.set_defaults(func=_rollback)

    return parser


def _print(message: str) -> None:
    sys.stdout.write(message)
    if not message.endswith("\n"):
        sys.stdout.write("\n")


def _emit_json(payload: dict) -> None:
    _print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _error(message: str) -> int:
    sys.stderr.write(message.rstrip() + "\n")
    return 1


def _render_report(report) -> dict:
    data = asdict(report)
    for key, value in list(data.items()):
        if isinstance(value, tuple):
            data[key] = list(value)
    return data


def _scan(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    report = scan_repository(root)
    if not report.is_git_repo:
        return _error(f"{root} is not a git repository")
    if args.json:
        _emit_json(_render_report(report))
    else:
        _print(f"branch: {report.branch}")
        _print(f"last commit: {report.last_commit}")
        if report.modified_files:
            _print("modified files:")
            for file in report.modified_files:
                _print(f"- {file}")
        else:
            _print("modified files: none")
    return 1 if report.dirty else 0


def _changelog(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    report = build_changelog(root, args.since_tag, force=args.force)
    if report.issues:
        return _error(report.issues[0])
    from .storage import atomic_write_text

    atomic_write_text(root / "CHANGELOG.md", report.content)
    if args.json:
        payload = _render_report(report)
        if args.force:
            payload["issues"] = []
        payload["written"] = True
        _emit_json(payload)
    else:
        _print(f"wrote {root / 'CHANGELOG.md'}")
    return 0


def _check(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    report = check_repository(root, allow_dirty=args.allow_dirty)
    if args.json:
        _emit_json(_render_report(report))
    if report.issues:
        for issue in report.issues:
            sys.stderr.write(issue + "\n")
        return 1
    if not args.json:
        _print("check passed")
    return 0


def _sync_core(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    report = sync_core_submodule(root, update=args.update)
    if args.json:
        _emit_json(_render_report(report))
    else:
        _print(f"submodule initialized: {report.exists}")
        _print(f"current sha: {report.current_sha or 'none'}")
        _print(f"remote sha: {report.remote_sha or 'none'}")
        _print(f"synced: {report.synced}")
        if report.lag_count > 0:
            _print(f"lag: {report.lag_count} commits behind")
        if report.updated:
            _print("submodule updated: yes")
            _print(f"tests passed after update: {report.tests_passed}")
        if report.issues:
            _print("issues:")
            for issue in report.issues:
                _print(f"- {issue}")
    return 1 if report.issues else 0


def _release(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    version = args.version
    if not validate_semver(version):
        return _error("version must match MAJOR.MINOR.PATCH")
    report = release_project(root, version, confirm=args.confirm, dry_run=args.dry_run)
    if report.issues:
        for issue in report.issues:
            sys.stderr.write(issue + "\n")
        return 1
    if args.dry_run:
        _print(f"dry-run: would write VERSION={version}, update CHANGELOG.md, create tag {release_tag(version)}")
        return 0
    _print(f"released {version}")
    return 0


def _rollback(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    version = args.version
    if not validate_semver(version):
        return _error("version must match MAJOR.MINOR.PATCH")
    report = rollback_project(root, version, confirm=args.confirm, dry_run=args.dry_run)
    if report.issues:
        for issue in report.issues:
            sys.stderr.write(issue + "\n")
        return 1
    if args.dry_run:
        _print(f"dry-run: would delete tag {report.tag}")
        return 0
    _print(f"removed tag {report.tag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - defensive fallback
        return _error(str(exc))
