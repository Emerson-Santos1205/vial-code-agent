from __future__ import annotations

from pathlib import Path
import subprocess

from .domain import Commit, TAG_PREFIX, release_tag


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
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def current_branch(root: Path) -> str:
    result = run_git(root, "branch", "--show-current", check=False)
    branch = result.stdout.strip()
    if branch:
        return branch
    head = run_git(root, "rev-parse", "--short", "HEAD", check=False)
    sha = head.stdout.strip()
    return f"HEAD ({sha})" if sha else "HEAD"


def last_commit(root: Path) -> str:
    result = run_git(root, "log", "-1", "--pretty=format:%h %s", check=False)
    text = result.stdout.strip()
    return text or "no commits yet"


def modified_files(root: Path) -> list[str]:
    result = run_git(root, "status", "--porcelain=v1", check=False)
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


def tag_exists(root: Path, version_or_tag: str) -> bool:
    tag = version_or_tag if version_or_tag.startswith(TAG_PREFIX) else release_tag(version_or_tag)
    result = run_git(root, "tag", "--list", tag, check=False)
    return tag in {line.strip() for line in result.stdout.splitlines()}


def latest_tag(root: Path) -> str | None:
    result = run_git(root, "tag", "--list", f"{TAG_PREFIX}*", "--sort=-version:refname", check=False)
    tags = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return tags[0] if tags else None


def commits_since(root: Path, since_tag: str | None) -> list[Commit]:
    args = ["log", "--pretty=format:%H%x00%s"]
    if since_tag:
        args.append(f"{since_tag}..HEAD")
    result = run_git(root, *args, check=False)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    commits: list[Commit] = []
    for line in reversed(lines):
        sha, subject = line.split("\x00", 1)
        commits.append(Commit(sha=sha, subject=subject))
    return commits


def create_annotated_tag(root: Path, tag: str, message: str) -> None:
    run_git(root, "tag", "-a", tag, "-m", message)


def delete_tag(root: Path, tag: str) -> None:
    run_git(root, "tag", "-d", tag)
