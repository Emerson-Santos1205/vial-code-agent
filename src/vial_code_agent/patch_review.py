"""Pre-application review gate for generated patches."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .patches import PatchApplier, PatchError


@dataclass(frozen=True)
class PatchReview:
    passed: bool
    checks: dict[str, bool]
    reason: str = ""


class PatchReviewGate:
    """Run deterministic checks before an authorized patch application."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def review(self, patch: str, allowed_paths: set[str] | None = None,
               forbidden_paths: set[str] | None = None) -> PatchReview:
        checks = {
            "parseable": False,
            "allowed_paths": False,
            "workspace_containment": False,
            "no_symlink_escape": False,
            "no_test_changes": False,
            "not_no_op": False,
        }
        applier = PatchApplier(self.root)
        try:
            paths = applier.paths(patch)
            checks["parseable"] = bool(paths)
            applier.validate(patch, allowed_paths)
            checks["allowed_paths"] = True
            checks["workspace_containment"] = True
            checks["no_symlink_escape"] = True
            checks["not_no_op"] = True
            forbidden = forbidden_paths or set()
            changed_forbidden = paths & forbidden
            checks["no_test_changes"] = not changed_forbidden
            if changed_forbidden:
                return PatchReview(False, checks,
                                   "patch changes forbidden files: "
                                   + ", ".join(sorted(changed_forbidden)))
        except PatchError as error:
            reason = str(error)
            if "no-op" in reason:
                checks["not_no_op"] = False
            if "escapes workspace" in reason or "symlink" in reason:
                checks["workspace_containment"] = False
                checks["no_symlink_escape"] = False
            return PatchReview(False, checks, reason)
        return PatchReview(all(checks.values()), checks)
