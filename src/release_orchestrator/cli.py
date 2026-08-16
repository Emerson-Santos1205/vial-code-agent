from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import (
    commits_since,
    current_branch,
    has_dirty_tree,
    is_git_repo,
    last_commit,
    latest_tag,
    modified_files,
    release_tag,
    render_changelog,
    run_git,
    run_test_suite,
    secret_files,
    tag_exists,
    test_files,
    validate_semver,
    write_text,
)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=Path.cwd())
    common.add_argument("--dry-run", action="store_true")

    parser = argparse.ArgumentParser(prog="release-orchestrator", parents=[common])
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", parents=[common])
    scan.set_defaults(func=_scan)

    changelog = subparsers.add_parser("changelog", parents=[common])
    changelog.add_argument("since_tag")
    changelog.add_argument("--force", action="store_true")
    changelog.set_defaults(func=_changelog)

    check = subparsers.add_parser("check", parents=[common])
    check.add_argument("--allow-dirty", action="store_true")
    check.set_defaults(func=_check)

    release = subparsers.add_parser("release", parents=[common])
    release.add_argument("version")
    release.set_defaults(func=_release)

    rollback = subparsers.add_parser("rollback", parents=[common])
    rollback.add_argument("version")
    rollback.add_argument("--confirm", action="store_true")
    rollback.set_defaults(func=_rollback)

    return parser


def _print(message: str) -> None:
    sys.stdout.write(message)
    if not message.endswith("\n"):
        sys.stdout.write("\n")


def _error(message: str) -> int:
    sys.stderr.write(message.rstrip() + "\n")
    return 1


def _scan(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    if not is_git_repo(root):
        return _error(f"{root} is not a git repository")
    files = modified_files(root)
    _print(f"branch: {current_branch(root)}")
    _print(f"last commit: {last_commit(root)}")
    if files:
        _print("modified files:")
        for file in files:
            _print(f"- {file}")
        return 1
    _print("modified files: none")
    return 0


def _changelog(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    output = root / "CHANGELOG.md"
    if output.exists() and not args.force and not args.dry_run:
        return _error("CHANGELOG.md already exists; use --force to overwrite")
    commits = commits_since(root, args.since_tag)
    content = render_changelog(args.since_tag, commits, args.since_tag)
    if args.dry_run:
        _print(content.rstrip())
        return 0
    write_text(output, content)
    _print(f"wrote {output.name}")
    return 0


def _check(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    issues: list[str] = []
    if not is_git_repo(root):
        issues.append("not a git repository")
    if not (root / "README.md").is_file():
        issues.append("missing README.md")
    if not test_files(root):
        issues.append("no tests found")
    secrets = secret_files(root)
    if secrets:
        issues.append("secret files present: " + ", ".join(sorted(str(p.relative_to(root)) for p in secrets)))
    if not args.allow_dirty and is_git_repo(root) and has_dirty_tree(root):
        issues.append("working tree is dirty")
    suite = run_test_suite(root)
    ran_tests = "Ran 0 tests" not in suite.stdout
    if suite.returncode != 0 or not ran_tests:
        issues.append("test suite failed")
        if suite.stdout:
            _print(suite.stdout.rstrip())
        if suite.stderr:
            sys.stderr.write(suite.stderr)
    if issues:
        for issue in issues:
            sys.stderr.write(issue + "\n")
        return 1
    _print("check passed")
    return 0


def _release(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    version = args.version
    if not validate_semver(version):
        return _error("version must match MAJOR.MINOR.PATCH")
    if not is_git_repo(root):
        return _error("not a git repository")
    if has_dirty_tree(root):
        return _error("working tree must be clean before release")
    if not (root / "README.md").is_file():
        return _error("missing README.md")
    if not test_files(root):
        return _error("no tests found")
    suite = run_test_suite(root)
    ran_tests = "Ran 0 tests" not in suite.stdout
    if suite.returncode != 0 or not ran_tests:
        if suite.stdout:
            _print(suite.stdout.rstrip())
        if suite.stderr:
            sys.stderr.write(suite.stderr)
        return 1
    tag = release_tag(version)
    if tag_exists(root, version):
        return _error(f"tag {tag} already exists")
    if args.dry_run:
        _print(f"dry-run: would write VERSION={version}, update CHANGELOG.md, create tag {tag}")
        return 0
    previous_tag = latest_tag(root)
    write_text(root / "VERSION", version + "\n")
    commits = commits_since(root, previous_tag)
    changelog = render_changelog(version, commits, previous_tag or "initial release")
    write_text(root / "CHANGELOG.md", changelog)
    run_git(root, "tag", "-a", tag, "-m", f"release-orchestrator {version}")
    _print(f"released {version}")
    return 0


def _rollback(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    version = args.version
    if not validate_semver(version):
        return _error("version must match MAJOR.MINOR.PATCH")
    if not args.confirm:
        return _error("rollback requires --confirm")
    if not is_git_repo(root):
        return _error("not a git repository")
    tag = release_tag(version)
    if args.dry_run:
        _print(f"dry-run: would delete tag {tag}")
        return 0
    if not tag_exists(root, version):
        return _error(f"tag {tag} not found")
    run_git(root, "tag", "-d", tag)
    _print(f"removed tag {tag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
