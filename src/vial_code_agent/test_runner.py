from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .processes import process_group_kwargs, terminate_process_tree


@dataclass(frozen=True)
class TestResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float

    @property
    def passed(self) -> bool:
        return self.returncode == 0


_terminate_process_tree = terminate_process_tree


def run_tests(root: Path, command: list[str], timeout_seconds: int = 120) -> TestResult:
    started = time.monotonic()
    popen_kwargs: dict[str, object] = {
        "cwd": root,
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
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return TestResult(
            tuple(command),
            process.returncode,
            stdout,
            stderr,
            time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as error:
        if process is not None:
            _terminate_process_tree(process)
        return TestResult(
            tuple(command),
            124,
            error.stdout if isinstance(error.stdout, str) else (
                error.stdout or b"").decode("utf-8", errors="replace"),
            error.stderr if isinstance(error.stderr, str) else (
                error.stderr or b"").decode("utf-8", errors="replace"),
            time.monotonic() - started,
        )
