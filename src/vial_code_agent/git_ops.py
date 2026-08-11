from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


class GitWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def run(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        if result.returncode:
            raise GitError(result.stderr.strip() or result.stdout.strip() or "git command failed")
        return result.stdout

    def status(self) -> str:
        return self.run("status", "--short")

    def branch(self, name: str, create: bool = False) -> str:
        return self.run("switch", "-c" if create else "", name) if create else self.run("switch", name)

    def create_branch(self, name: str) -> str:
        return self.run("switch", "-c", name)

    def commit(self, message: str) -> str:
        if not message.strip():
            raise ValueError("commit message is empty")
        self.run("add", "-A")
        return self.run("commit", "-m", message)

    def github(self, *args: str) -> str:
        """Delegate GitHub operations to authenticated `gh`; never handles tokens."""
        result = subprocess.run(
            ["gh", *args], cwd=self.root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        if result.returncode:
            raise GitError(result.stderr.strip() or "gh command failed")
        return result.stdout
