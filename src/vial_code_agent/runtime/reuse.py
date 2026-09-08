"""Cognitive reuse (RFC-008) with enhanced signature."""
from __future__ import annotations

import hashlib
import json
import platform
import sys
from pathlib import Path
from typing import Any

from ..persistence import Repository


def _enhanced_signature(task: Any, base_commit: str = "",
                        workspace_root: Path | None = None,
                        toolchain_id: str = "",
                        file_digests: dict[str, str] | None = None) -> str:
    """Generate a rich reuse signature incorporating workspace context.

    Includes:
    - task.op + task.args (semantic operation)
    - base_commit (git state)
    - workspace file digest (relevant files)
    - toolchain identity (Python version, platform)
    - dependency lock hash (if available)

    This prevents reuse from semantically different workspaces that happen
    to have the same task and file set.
    """
    sig_data = {
        "op": getattr(task, "op", ""),
        "args": getattr(task, "args", None),
        "base_commit": base_commit,
        "toolchain": toolchain_id or f"{sys.version}:{platform.system()}",
    }

    # Add workspace file digest for relevant files
    if file_digests:
        sig_data["file_digests"] = file_digests
    elif workspace_root is not None:
        try:
            files = getattr(task, "required", [])
            if files:
                digest = hashlib.sha256()
                for file_ref in sorted(files):
                    # Extract file path from state reference
                    if file_ref.startswith("file:"):
                        file_path = workspace_root / file_ref[5:]
                        if file_path.is_file():
                            digest.update(file_ref.encode())
                            digest.update(file_path.read_bytes())
                sig_data["workspace_digest"] = digest.hexdigest()
        except (OSError, ValueError):
            pass

    # Add dependency lock hash if available
    if workspace_root is not None:
        for lock_file in ("requirements.txt.lock", "poetry.lock", "pdm.lock",
                          "package-lock.json", "yarn.lock", "Cargo.lock"):
            lock_path = workspace_root / lock_file
            if lock_path.is_file():
                try:
                    lock_content = lock_path.read_bytes()
                    sig_data["dependency_hash"] = hashlib.sha256(
                        lock_content).hexdigest()[:16]
                    break
                except OSError:
                    pass

    return json.dumps(sig_data, sort_keys=True)


class CognitiveReuseMixin:
    """Cognitive reuse methods with enhanced workspace-aware signatures."""

    def lookup_reuse(self, task: Any) -> tuple[Any | None, str]:
        """Enhanced lookup with workspace-aware signature."""
        # Get workspace context for signature enrichment
        base_commit = getattr(self, "base_commit", "")
        workspace_root = getattr(self, "workspace_root", None)

        # Compute workspace digest if available
        workspace_digest = ""
        dependency_hash = ""
        if workspace_root is not None:
            # Compute workspace digest from relevant files
            try:
                files = getattr(task, "required", [])
                if files:
                    digest = hashlib.sha256()
                    for file_ref in sorted(files):
                        if file_ref.startswith("file:"):
                            file_path = workspace_root / file_ref[5:]
                            if file_path.is_file():
                                digest.update(file_ref.encode())
                                digest.update(file_path.read_bytes())
                    workspace_digest = digest.hexdigest()
            except (OSError, ValueError):
                pass

            # Compute dependency lock hash
            for lock_file in ("requirements.txt.lock", "poetry.lock", "pdm.lock",
                              "package-lock.json", "yarn.lock", "Cargo.lock"):
                lock_path = workspace_root / lock_file
                if lock_path.is_file():
                    try:
                        lock_content = lock_path.read_bytes()
                        dependency_hash = hashlib.sha256(
                            lock_content).hexdigest()[:16]
                        break
                    except OSError:
                        pass

        entry, outcome = self.reuse_engine.lookup(  # type: ignore[attr-defined]
            task, base_commit, workspace_digest, dependency_hash)
        if outcome == "hit":
            self.reuse_engine.reuse_hits += 1  # type: ignore[attr-defined]
        return entry, outcome

    def store_reuse(self, task: Any, outcome: Any, quality: float,
                    context: Any) -> Any:
        """Store with workspace-aware signature."""
        base_commit = getattr(self, "base_commit", "")
        workspace_root = getattr(self, "workspace_root", None)

        workspace_digest = ""
        dependency_hash = ""
        if workspace_root is not None:
            try:
                files = getattr(task, "required", [])
                if files:
                    digest = hashlib.sha256()
                    for file_ref in sorted(files):
                        if file_ref.startswith("file:"):
                            file_path = workspace_root / file_ref[5:]
                            if file_path.is_file():
                                digest.update(file_ref.encode())
                                digest.update(file_path.read_bytes())
                    workspace_digest = digest.hexdigest()
            except (OSError, ValueError):
                pass

            for lock_file in ("requirements.txt.lock", "poetry.lock", "pdm.lock",
                              "package-lock.json", "yarn.lock", "Cargo.lock"):
                lock_path = workspace_root / lock_file
                if lock_path.is_file():
                    try:
                        lock_content = lock_path.read_bytes()
                        dependency_hash = hashlib.sha256(
                            lock_content).hexdigest()[:16]
                        break
                    except OSError:
                        pass

        self.reuse_engine.recomputes += 1  # type: ignore[attr-defined]
        return self.reuse_engine.store(  # type: ignore[attr-defined]
            task, outcome, quality, context,
            provenance=f"org:{self.org_id}:runtime",  # type: ignore[attr-defined]
            base_commit=base_commit,
            workspace_digest=workspace_digest,
            dependency_hash=dependency_hash,
        )

    def reuse_stats(self) -> dict[str, Any]:
        return self.reuse_engine.stats()  # type: ignore[attr-defined]
