from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    """Run commands with an explicit allowlist; never unrestricted by default."""

    def __init__(self, root: Path, allowed: set[str] | None = None, unsafe: bool = False) -> None:
        self.root = root.resolve()
        self.allowed = allowed or {"python", "python.exe", "pytest", "unittest", "git", "npm", "node"}
        self.unsafe = unsafe

    def run(self, command: list[str], timeout: int = 120) -> CommandResult:
        if not command:
            raise ValueError("command is empty")
        executable = Path(command[0]).name.lower()
        if not self.unsafe and executable not in {name.lower() for name in self.allowed}:
            raise PermissionError(f"command is not allowlisted: {command[0]}")
        completed = subprocess.run(
            command, cwd=self.root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
        return CommandResult(tuple(command), completed.returncode, completed.stdout, completed.stderr)

    @staticmethod
    def parse(command: str) -> list[str]:
        return shlex.split(command, posix=False)
