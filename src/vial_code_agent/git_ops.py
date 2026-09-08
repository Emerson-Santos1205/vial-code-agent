from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


# Risk levels for git command categories
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_CRITICAL = "critical"


@dataclass(frozen=True)
class GitCommandPolicy:
    """Policy for a specific git command."""
    category: str
    risk: str
    requires_consensus: bool = False
    requires_approval: bool = False


class GitPolicy:
    """Categorizes git commands by risk level.

    Categories:
    - READ: read-only operations (status, diff, log)
    - LOCAL_MUTATION: local changes (add, commit, branch)
    - DESTRUCTIVE: irreversible operations (reset, clean)
    - REMOTE: network operations (push, pull, fetch)
    - CONFIGURATION: config changes (config, remote)
    """

    # Read-only commands
    READ_COMMANDS = {
        "status", "diff", "log", "show", "blame", "ls-files",
        "ls-tree", "rev-parse", "rev-list", "describe", "name-rev",
        "for-each-ref", "branch",  # branch without -d/-D/-m/-M
    }

    # Local mutation commands
    LOCAL_MUTATION_COMMANDS = {
        "add", "rm", "mv", "commit", "stash", "reset",  # reset without --hard
        "checkout",  # checkout without --force
        "switch", "restore", "tag", "merge", "rebase",
    }

    # Destructive commands (irreversible)
    DESTRUCTIVE_COMMANDS = {
        "clean",  # clean -fd
        "reset",  # reset --hard
        "checkout",  # checkout --force
        "switch",  # switch --discard-changes
        "restore",  # restore --staged --worktree
        "rebase",  # rebase --abort (can lose work)
        "push",  # push --force
    }

    # Remote commands
    REMOTE_COMMANDS = {
        "push", "pull", "fetch", "clone", "remote",
    }

    # Configuration commands
    CONFIGURATION_COMMANDS = {
        "config", "remote",
    }

    # Risk levels by category
    CATEGORY_RISK = {
        "read": RISK_LOW,
        "local_mutation": RISK_MEDIUM,
        "destructive": RISK_CRITICAL,
        "remote": RISK_HIGH,
        "configuration": RISK_MEDIUM,
    }

    def classify(self, args: list[str]) -> GitCommandPolicy:
        """Classify a git command by its risk level."""
        if not args:
            return GitCommandPolicy("read", RISK_LOW)

        subcommand = args[0].lstrip("-")  # handle --flags

        # Check for destructive flags
        if self._is_destructive(subcommand, args):
            return GitCommandPolicy(
                "destructive",
                RISK_CRITICAL,
                requires_consensus=True,
                requires_approval=True,
            )

        # Check for remote operations
        if subcommand in self.REMOTE_COMMANDS:
            return GitCommandPolicy(
                "remote",
                RISK_HIGH,
                requires_consensus=True,
            )

        # Check for configuration
        if subcommand in self.CONFIGURATION_COMMANDS:
            return GitCommandPolicy(
                "configuration",
                RISK_MEDIUM,
                requires_consensus=True,
            )

        # Check for local mutation
        if subcommand in self.LOCAL_MUTATION_COMMANDS:
            return GitCommandPolicy(
                "local_mutation",
                RISK_MEDIUM,
                requires_consensus=True,
            )

        # Default to read
        return GitCommandPolicy("read", RISK_LOW)

    def _is_destructive(self, subcommand: str, args: list[str]) -> bool:
        """Check if the command is destructive based on flags."""
        flags = " ".join(args)

        # Destructive patterns
        if subcommand == "reset" and "--hard" in flags:
            return True
        if subcommand == "clean" and any(f in flags for f in ["-f", "-fd", "-fda"]):
            return True
        if subcommand == "checkout" and "--force" in flags:
            return True
        if subcommand == "switch" and "--discard-changes" in flags:
            return True
        if subcommand == "restore" and "--staged" in flags and "--worktree" in flags:
            return True
        if subcommand == "push" and any(f in flags for f in ["--force", "-f"]):
            return True
        if subcommand == "rebase" and "--abort" in flags:
            return True

        return False


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
