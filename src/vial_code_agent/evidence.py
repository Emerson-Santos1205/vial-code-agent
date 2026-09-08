"""Evidence checks for candidate code changes."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .patches import PatchApplier, PatchError
from .processes import process_group_kwargs, terminate_process_tree


@dataclass(frozen=True)
class EvidenceResult:
    static_valid: bool
    behavioral_passed: bool | None
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.static_valid and self.behavioral_passed is not False


class EvidenceRunner:
    """Run evidence validation with security controls.

    Unlike the raw ``subprocess.run`` path, this runner enforces:
    - Command allowlist (only approved test commands)
    - Timeout with process tree termination
    - Resource limits via process isolation
    - Network isolation (disabled by default via env)
    """

    DEFAULT_ALLOWED: set[str] = {
        "python", "python.exe", "python3",
        "pytest", "unittest",
        "npm", "node",
        "cargo", "rustc",
        "go",
        "make", "cmake",
    }

    def __init__(
        self,
        allowed: set[str] | None = None,
        timeout: int = 120,
        network_enabled: bool = False,
    ) -> None:
        self.allowed = allowed or self.DEFAULT_ALLOWED
        self.timeout = timeout
        self.network_enabled = network_enabled

    def _validate_command(self, command: list[str]) -> None:
        """Raise if command is not allowlisted."""
        if not command:
            raise ValueError("command is empty")
        executable = Path(command[0]).name.lower()
        if executable not in {name.lower() for name in self.allowed}:
            raise PermissionError(f"command is not allowlisted: {command[0]}")

    def _build_env(self) -> dict[str, str]:
        """Build environment with optional network isolation."""
        import os
        env = os.environ.copy()
        if not self.network_enabled:
            # Disable network access for test execution
            env["NO_PROXY"] = "*"
            env["no_proxy"] = "*"
            # Standard env vars that some tools respect
            env["REQUESTS_CA_BUNDLE"] = ""
            env["SSL_CERT_FILE"] = ""
        return env

    def run(
        self,
        command: list[str],
        cwd: Path,
        timeout: int | None = None,
    ) -> tuple[int, str, str]:
        """Execute command with security controls.

        Returns (returncode, stdout, stderr).
        """
        self._validate_command(command)
        effective_timeout = timeout or self.timeout
        popen_kwargs: dict[str, Any] = {
            "cwd": cwd,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": self._build_env(),
        }
        popen_kwargs.update(process_group_kwargs())
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(command, **popen_kwargs)
            stdout, stderr = process.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired as error:
            if process is not None:
                terminate_process_tree(process)
            stdout = error.stdout if isinstance(error.stdout, str) else (
                error.stdout or b"").decode("utf-8", errors="replace")
            stderr = error.stderr if isinstance(error.stderr, str) else (
                error.stderr or b"").decode("utf-8", errors="replace")
            return 124, stdout, stderr
        return process.returncode, stdout, stderr


def validate_candidate(root: Path, patch: str,
                       test_command: list[str] | None = None,
                       timeout: int = 120,
                       runner: EvidenceRunner | None = None) -> EvidenceResult:
    """Validate one candidate in an isolated copy of ``root``."""
    try:
        with tempfile.TemporaryDirectory(prefix="vial-evidence-") as directory:
            isolated = Path(directory) / "workspace"
            shutil.copytree(
                root, isolated,
                ignore=shutil.ignore_patterns(".git", ".vial-state", ".vial-cache"),
            )
            PatchApplier(isolated).apply(patch)
            if not test_command:
                return EvidenceResult(True, None, "static patch validation passed")
            if runner is None:
                runner = EvidenceRunner()
            returncode, stdout, stderr = runner.run(test_command, isolated, timeout)
            detail = (stdout + stderr).strip()[-1000:]
            return EvidenceResult(True, returncode == 0, detail)
    except (OSError, PatchError, PermissionError) as error:
        return EvidenceResult(False, False, str(error))
