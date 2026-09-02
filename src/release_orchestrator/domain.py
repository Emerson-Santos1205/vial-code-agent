from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

TAG_PREFIX = "release-orchestrator-v"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
CONVENTIONAL_RE = re.compile(r"^(feat|fix|docs|test)(?:\([^)]+\))?!?:\s*(.+)$")
CATEGORIES = ("feat", "fix", "docs", "test", "other")


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str


@dataclass(frozen=True)
class ReleaseChanges:
    version: str
    since_tag: str
    commits: tuple[Commit, ...]
    grouped: dict[str, tuple[Commit, ...]]
    counts: dict[str, int]
    content: str


def release_tag(version: str) -> str:
    return f"{TAG_PREFIX}{version}"


def validate_semver(version: str) -> bool:
    return bool(SEMVER_RE.fullmatch(version))


def categorize_commit_subject(subject: str) -> str:
    match = CONVENTIONAL_RE.match(subject)
    return match.group(1) if match else "other"


def categorize_commits(commits: Sequence[Commit]) -> dict[str, list[Commit]]:
    grouped: dict[str, list[Commit]] = {category: [] for category in CATEGORIES}
    for commit in commits:
        grouped[categorize_commit_subject(commit.subject)].append(commit)
    return grouped


def _render_changelog(version: str, since_tag: str, grouped: dict[str, tuple[Commit, ...]]) -> str:
    lines = ["# Changelog", "", f"## {version}", f"_Changes since {since_tag}_", ""]
    any_commits = False
    for category in CATEGORIES:
        commits = grouped[category]
        if not commits:
            continue
        any_commits = True
        lines.append(f"### {category}")
        for commit in commits:
            lines.append(f"- {commit.sha[:7]} {commit.subject}")
        lines.append("")
    if not any_commits:
        lines.append("- No commits found.")
    elif lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def calculate_release_changes(
    version: str,
    commits: Sequence[Commit],
    since_tag: str,
) -> ReleaseChanges:
    grouped_list = categorize_commits(commits)
    grouped = {category: tuple(items) for category, items in grouped_list.items()}
    counts = {category: len(items) for category, items in grouped.items()}
    content = _render_changelog(version, since_tag, grouped)
    return ReleaseChanges(
        version=version,
        since_tag=since_tag,
        commits=tuple(commits),
        grouped=grouped,
        counts=counts,
        content=content,
    )


def validate_release_transition(
    *,
    version: str,
    repo_issues: Sequence[str],
    tests_ok: bool,
    tag_exists: bool,
    confirm: bool,
) -> list[str]:
    issues = list(repo_issues)
    if not validate_semver(version):
        issues.append("version must match MAJOR.MINOR.PATCH")
    if not confirm:
        issues.append("release requires --confirm")
    if not tests_ok:
        issues.append("test suite failed")
    if tag_exists:
        issues.append(f"tag {release_tag(version)} already exists")
    return issues


def validate_rollback_transition(
    *,
    version: str,
    confirm: bool,
    tag_exists: bool,
    dry_run: bool,
) -> list[str]:
    issues = []
    if not validate_semver(version):
        issues.append("version must match MAJOR.MINOR.PATCH")
    if not confirm and not dry_run:
        issues.append("rollback requires --confirm")
    if not tag_exists:
        issues.append(f"tag {release_tag(version)} not found")
    return issues
