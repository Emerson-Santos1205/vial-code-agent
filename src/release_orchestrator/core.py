from __future__ import annotations

from dataclasses import dataclass
import re
import subprocess
import sys
from pathlib import Path


TAG_PREFIX = "release-orchestrator-v"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
CONVENTIONAL_RE = re.compile(r"^(feat|fix|docs|test)(\([^)]+\))?:\s*(.+)$")


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str


def run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=check,
    )


def is_git_repo(root: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        text=True,
        capture_output=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def current_branch(root: Path) -> str:
    result = run_git(root, "branch", "--show-current", check=False)
    branch = result.stdout.strip()
    if branch:
        return branch
    head = run_git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    return f"HEAD ({head})"


def last_commit(root: Path) -> str:
    result = run_git(root, "log", "-1", "--pretty=format:%h %s", check=False)
    text = result.stdout.strip()
    return text or "no commits yet"


def modified_files(root: Path) -> list[str]:
    result = run_git(root, "status", "--porcelain")
    files: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return files


def has_dirty_tree(root: Path) -> bool:
    return bool(modified_files(root))


def existing_tag(root: Path, version: str) -> str:
    return f"{TAG_PREFIX}{version}"


def tag_exists(root: Path, version: str) -> bool:
    result = subprocess.run(
        ["git", "tag", "--list", existing_tag(root, version)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return existing_tag(root, version) in {line.strip() for line in result.stdout.splitlines()}


def latest_tag(root: Path) -> str | None:
    result = run_git(root, "tag", "--list", f"{TAG_PREFIX}*", "--sort=-version:refname", check=False)
    tags = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return tags[0] if tags else None


def commits_since(root: Path, since_tag: str | None) -> list[Commit]:
    args = ["log", "--pretty=format:%H%x00%s"]
    if since_tag:
        args.append(f"{since_tag}..HEAD")
    result = run_git(root, *args, check=False)
    commits: list[Commit] = []
    for line in reversed([line for line in result.stdout.splitlines() if line.strip()]):
        sha, subject = line.split("\x00", 1)
        commits.append(Commit(sha=sha, subject=subject))
    return commits


def categorize_commits(commits: list[Commit]) -> dict[str, list[Commit]]:
    grouped: dict[str, list[Commit]] = {"feat": [], "fix": [], "docs": [], "test": [], "other": []}
    for commit in commits:
        match = CONVENTIONAL_RE.match(commit.subject)
        category = match.group(1) if match else "other"
        grouped[category].append(commit)
    return grouped


def render_changelog(version: str, commits: list[Commit], since_tag: str) -> str:
    grouped = categorize_commits(commits)
    lines = ["# Changelog", "", f"## {version}", f"_Changes since {since_tag}_", ""]
    for category in ("feat", "fix", "docs", "test", "other"):
        items = grouped[category]
        if not items:
            continue
        lines.append(f"### {category}")
        for commit in items:
            lines.append(f"- {commit.sha[:7]} {commit.subject}")
        lines.append("")
    if lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def validate_semver(version: str) -> bool:
    return bool(SEMVER_RE.fullmatch(version))


def release_tag(version: str) -> str:
    return f"{TAG_PREFIX}{version}"


def secret_files(root: Path) -> list[Path]:
    findings = []
    for name in (".env", "credentials.json"):
        findings.extend(root.rglob(name))
    return findings


def test_files(root: Path) -> list[Path]:
    tests_dir = root / "tests"
    if not tests_dir.exists():
        return []
    return [path for path in tests_dir.rglob("test*.py") if path.is_file()]


def run_test_suite(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=root,
        text=True,
        capture_output=True,
    )
