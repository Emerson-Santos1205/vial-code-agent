from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .processes import process_group_kwargs, terminate_process_tree


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


_terminate_process_tree = terminate_process_tree


class CommandRunner:
    """Run commands with an explicit allowlist; never unrestricted by default."""

    def __init__(self, root: Path, allowed: set[str] | None = None, unsafe: bool = False) -> None:
        self.root = root.resolve()
        self.allowed = allowed or {
            "python", "python.exe", "python3", "pytest", "unittest", "git", "npm", "node"}
        self.unsafe = unsafe

    def run(self, command: list[str], timeout: int = 120) -> CommandResult:
        if not command:
            raise ValueError("command is empty")
        executable = Path(command[0]).name.lower()
        if not self.unsafe and executable not in {name.lower() for name in self.allowed}:
            raise PermissionError(f"command is not allowlisted: {command[0]}")
        popen_kwargs: dict[str, object] = {
            "cwd": self.root,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        popen_kwargs.update(process_group_kwargs())
        process: subprocess.Popen[str] | None = None
        try:
            # type: ignore[call-overload]
            process = subprocess.Popen(command, **popen_kwargs)
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            if process is not None:
                _terminate_process_tree(process)
            stdout = error.stdout if isinstance(error.stdout, str) else (
                error.stdout or b"").decode("utf-8", errors="replace")
            stderr = error.stderr if isinstance(error.stderr, str) else (
                error.stderr or b"").decode("utf-8", errors="replace")
            return CommandResult(tuple(command), 124, stdout, stderr)
        return CommandResult(tuple(command), process.returncode, stdout, stderr)

    @staticmethod
    def parse(command: str) -> list[str]:
        return shlex.split(command, posix=False)
