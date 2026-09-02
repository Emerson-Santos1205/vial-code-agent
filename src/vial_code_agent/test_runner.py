from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


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


def run_tests(root: Path, command: list[str], timeout_seconds: int = 120) -> TestResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return TestResult(
            tuple(command),
            completed.returncode,
            completed.stdout,
            completed.stderr,
            time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as error:
        return TestResult(
            tuple(command),
            124,
            error.stdout if isinstance(error.stdout, str) else (error.stdout or b"").decode("utf-8", errors="replace"),
            error.stderr if isinstance(error.stderr, str) else (error.stderr or b"").decode("utf-8", errors="replace"),
            time.monotonic() - started,
        )
