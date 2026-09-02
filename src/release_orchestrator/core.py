from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess

from .domain import (
    Commit,
    ReleaseChanges,
    calculate_release_changes,
    release_tag,
    validate_release_transition,
    validate_rollback_transition,
)
from .git import (
    check_submodule_drift,
    commits_since,
    create_annotated_tag,
    current_branch,
    delete_tag,
    has_dirty_tree,
    is_git_repo,
    last_commit,
    latest_tag,
    modified_files,
    tag_exists,
    update_submodule,
)
from .storage import atomic_write_text

SECRET_FILENAMES = (".env", "credentials.json")
CHANGELOG_OVERWRITE_ISSUE = "CHANGELOG.md already exists; use --force to overwrite"


@dataclass(frozen=True)
class ScanReport:
    is_git_repo: bool
    branch: str
    last_commit: str
    modified_files: tuple[str, ...]
    dirty: bool


@dataclass(frozen=True)
class SubmoduleReport:
    exists: bool
    dirty: bool
    current_sha: str
    remote_sha: str
    lag_count: int
    synced: bool
    updated: bool
    tests_passed: bool
    issues: tuple[str, ...]


@dataclass(frozen=True)
class CheckReport:
    issues: tuple[str, ...]
    readme_exists: bool
    tests_found: int
    tests_ran: bool
    tests_passed: bool
    suite_returncode: int
    suite_stdout: str
    suite_stderr: str


@dataclass(frozen=True)
class ChangelogReport:
    issues: tuple[str, ...]
    version: str
    since_tag: str
    path: str
    content: str
    counts: dict[str, int]
    written: bool


@dataclass(frozen=True)
class ReleaseReport:
    issues: tuple[str, ...]
    version: str
    tag: str
    version_path: str
    changelog_path: str
    dry_run: bool
    written: bool
    tag_created: bool
    changes: ReleaseChanges


@dataclass(frozen=True)
class RollbackReport:
    issues: tuple[str, ...]
    version: str
    tag: str
    dry_run: bool
    removed: bool


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def write_text(path: Path, text: str) -> None:
    atomic_write_text(path, text)


def existing_tag(version: str) -> str:
    return release_tag(version)


def render_changelog(version: str, commits: Sequence[Commit], since_tag: str) -> str:
    return calculate_release_changes(version, commits, since_tag).content


def secret_files(root: Path) -> list[Path]:
    findings: list[Path] = []
    for name in SECRET_FILENAMES:
        findings.extend(path for path in root.rglob(name) if path.is_file())
    return findings


def has_readme(root: Path) -> bool:
    return (root / "README.md").is_file()


def test_files(root: Path) -> list[Path]:
    tests_dir = root / "tests"
    if not tests_dir.is_dir():
        return []
    files: list[Path] = []
    for pattern in ("test*.py", "*_test.py"):
        files.extend(path for path in tests_dir.rglob(pattern) if path.is_file())
    return sorted({path for path in files}, key=str)


def run_test_suite(root: Path) -> CompletedProcess[str]:
    import os
    import subprocess

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def repo_health_issues(root: Path, *, allow_dirty: bool = False) -> list[str]:
    issues: list[str] = []
    if not is_git_repo(root):
        issues.append("not a git repository")
    if not has_readme(root):
        issues.append("missing README.md")
    if not test_files(root):
        issues.append("no tests found")
    secrets = secret_files(root)
    if secrets:
        issues.append("secret files present: " + ", ".join(sorted(str(path.relative_to(root)) for path in secrets)))
    if not allow_dirty and is_git_repo(root) and has_dirty_tree(root):
        issues.append("working tree is dirty")
    sub_dir = root / "vendor" / "vial-core"
    if not sub_dir.is_dir():
        issues.append("submodule vendor/vial-core not initialized")
    elif has_dirty_tree(sub_dir):
        issues.append("submodule vendor/vial-core is dirty")
    return issues


def scan_repository(root: Path) -> ScanReport:
    repo = is_git_repo(root)
    return ScanReport(
        is_git_repo=repo,
        branch=current_branch(root) if repo else "",
        last_commit=last_commit(root) if repo else "",
        modified_files=tuple(modified_files(root)) if repo else tuple(),
        dirty=has_dirty_tree(root) if repo else False,
    )


def sync_core_submodule(root: Path, *, update: bool = False) -> SubmoduleReport:
    drift = check_submodule_drift(root, "vendor/vial-core")
    issues: list[str] = []

    if not drift["exists"]:
        issues.append("submodule vendor/vial-core not initialized")
        return SubmoduleReport(
            exists=False, dirty=False, current_sha="", remote_sha="",
            lag_count=-1, synced=False, updated=False, tests_passed=False,
            issues=tuple(issues),
        )

    if drift["dirty"]:
        issues.append("submodule vendor/vial-core has uncommitted changes")

    updated = False
    tests_passed = True

    if update and drift["exists"] and not drift["dirty"]:
        success = update_submodule(root, "vendor/vial-core")
        if success:
            updated = True
            suite = run_test_suite(root)
            tests_passed = suite.returncode == 0 and "Ran 0 tests" not in suite.stdout
            if not tests_passed:
                issues.append("test suite failed after submodule update")
        else:
            issues.append("failed to update submodule vendor/vial-core")

    if updated:
        drift = check_submodule_drift(root, "vendor/vial-core")

    if not drift["synced"] and drift["remote_sha"]:
        lag = f" ({drift['lag_count']} commits behind)" if drift["lag_count"] > 0 else ""
        issues.append(f"submodule vendor/vial-core is out of sync with remote{lag}")

    return SubmoduleReport(
        exists=bool(drift["exists"]),
        dirty=bool(drift["dirty"]),
        current_sha=str(drift["current_sha"]),
        remote_sha=str(drift["remote_sha"]),
        lag_count=int(drift["lag_count"]),
        synced=bool(drift["synced"]),
        updated=updated,
        tests_passed=tests_passed,
        issues=tuple(issues),
    )


def build_changelog(
    root: Path,
    since_tag: str,
    *,
    version: str | None = None,
    force: bool = False,
) -> ChangelogReport:
    output = root / "CHANGELOG.md"
    issues: list[str] = []
    if not tag_exists(root, since_tag):
        issues.append(f"tag {since_tag} not found")
    if output.exists() and not force:
        issues.append(CHANGELOG_OVERWRITE_ISSUE)
    effective_version = version or since_tag
    changes = calculate_release_changes(effective_version, commits_since(root, since_tag), since_tag)
    return ChangelogReport(
        issues=tuple(issues),
        version=effective_version,
        since_tag=since_tag,
        path=str(output),
        content=changes.content,
        counts=changes.counts,
        written=False,
    )


def check_repository(root: Path, *, allow_dirty: bool = False) -> CheckReport:
    issues = repo_health_issues(root, allow_dirty=allow_dirty)
    tests = test_files(root)
    suite = run_test_suite(root)
    ran_tests = "Ran 0 tests" not in suite.stdout
    tests_passed = suite.returncode == 0 and ran_tests
    if not tests_passed:
        issues.append("test suite failed")
    return CheckReport(
        issues=tuple(issues),
        readme_exists=has_readme(root),
        tests_found=len(tests),
        tests_ran=ran_tests,
        tests_passed=tests_passed,
        suite_returncode=suite.returncode,
        suite_stdout=suite.stdout,
        suite_stderr=suite.stderr,
    )


def release_project(root: Path, version: str, *, confirm: bool = False, dry_run: bool = False) -> ReleaseReport:
    previous_tag = latest_tag(root)
    since_tag = previous_tag or "initial release"
    changes = calculate_release_changes(version, commits_since(root, previous_tag), since_tag)
    suite = run_test_suite(root)
    tests_ok = suite.returncode == 0 and "Ran 0 tests" not in suite.stdout
    repo_issues = repo_health_issues(root, allow_dirty=False)
    issues = validate_release_transition(
        version=version,
        repo_issues=repo_issues,
        tests_ok=tests_ok,
        tag_exists=tag_exists(root, release_tag(version)),
        confirm=confirm,
    )
    if issues:
        return ReleaseReport(
            issues=tuple(issues),
            version=version,
            tag=release_tag(version),
            version_path=str(root / "VERSION"),
            changelog_path=str(root / "CHANGELOG.md"),
            dry_run=dry_run,
            written=False,
            tag_created=False,
            changes=changes,
        )
    if dry_run:
        return ReleaseReport(
            issues=(),
            version=version,
            tag=release_tag(version),
            version_path=str(root / "VERSION"),
            changelog_path=str(root / "CHANGELOG.md"),
            dry_run=True,
            written=False,
            tag_created=False,
            changes=changes,
        )
    write_text(root / "VERSION", version + "\n")
    write_text(root / "CHANGELOG.md", changes.content)
    create_annotated_tag(root, release_tag(version), f"release-orchestrator {version}")
    return ReleaseReport(
        issues=(),
        version=version,
        tag=release_tag(version),
        version_path=str(root / "VERSION"),
        changelog_path=str(root / "CHANGELOG.md"),
        dry_run=False,
        written=True,
        tag_created=True,
        changes=changes,
    )


def rollback_project(root: Path, version: str, *, confirm: bool = False, dry_run: bool = False) -> RollbackReport:
    tag = release_tag(version)
    issues = validate_rollback_transition(version=version, confirm=confirm, tag_exists=tag_exists(root, tag), dry_run=dry_run)
    if issues:
        return RollbackReport(issues=tuple(issues), version=version, tag=tag, dry_run=dry_run, removed=False)
    if dry_run:
        return RollbackReport(issues=(), version=version, tag=tag, dry_run=True, removed=False)
    delete_tag(root, tag)
    return RollbackReport(issues=(), version=version, tag=tag, dry_run=False, removed=True)
