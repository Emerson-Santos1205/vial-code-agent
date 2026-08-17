"""Evidence checks for candidate code changes."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .patches import PatchApplier, PatchError


@dataclass(frozen=True)
class EvidenceResult:
    static_valid: bool
    behavioral_passed: bool | None
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.static_valid and self.behavioral_passed is not False


def validate_candidate(root: Path, patch: str,
                       test_command: list[str] | None = None,
                       timeout: int = 120) -> EvidenceResult:
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
            result = subprocess.run(
                test_command, cwd=isolated, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, check=False,
            )
            detail = (result.stdout + result.stderr).strip()[-1000:]
            return EvidenceResult(True, result.returncode == 0, detail)
    except (OSError, PatchError, subprocess.TimeoutExpired) as error:
        return EvidenceResult(False, False, str(error))
