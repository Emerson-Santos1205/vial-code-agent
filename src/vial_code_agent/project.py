"""Materialized project state and ΔState projection for agents.

A ``ProjectSnapshot`` carries deterministic facts (digest/size/lines per file)
plus an optional status map (``backend: complete``, ...). Agents consume only a
``ProjectDelta`` (changed/added/removed files) plus the relevant status instead
of full history. Everything here is deterministic code; no model invocation.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _file_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class FileEntry:
    path: str
    digest: str
    size: int
    lines: int

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "digest": self.digest,
                "size": self.size, "lines": self.lines}


@dataclass(frozen=True)
class ProjectSnapshot:
    project: str
    version: int
    digest: str
    files: dict[str, FileEntry] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)

    def delta(self, other: "ProjectSnapshot") -> "ProjectDelta":
        changed: list[tuple[str, str, str]] = []
        added: list[str] = []
        removed: list[str] = []
        for path, entry in other.files.items():
            if path not in self.files:
                added.append(path)
            elif self.files[path].digest != entry.digest:
                changed.append((path, self.files[path].digest, entry.digest))
        for path in self.files:
            if path not in other.files:
                removed.append(path)
        return ProjectDelta(
            self.project, other.version, changed, added, removed, other.status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "version": self.version,
            "digest": self.digest,
            "files": {path: entry.to_dict() for path, entry in self.files.items()},
            "status": dict(self.status),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProjectSnapshot":
        return cls(
            project=str(value["project"]),
            version=int(value["version"]),
            digest=str(value["digest"]),
            files={path: FileEntry(**entry)
                   for path, entry in value.get("files", {}).items()},
            status=dict(value.get("status") or {}),
        )


@dataclass(frozen=True)
class ProjectDelta:
    project: str
    version: int
    changed: list[tuple[str, str, str]]
    added: list[str]
    removed: list[str]
    status: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "version": self.version,
            "changed": [list(change) for change in self.changed],
            "added": list(self.added),
            "removed": list(self.removed),
            "status": dict(self.status),
        }


class ProjectStateStore:
    """Versioned materialized state with authorized status updates."""

    def __init__(self) -> None:
        self.snapshot: ProjectSnapshot | None = None
        self.authorized_actors: set[str] = set()

    def configure(self, authorized: set[str]) -> None:
        self.authorized_actors = set(authorized)

    def capture(self, root: Path, files: list[Path]) -> ProjectSnapshot:
        entries: dict[str, FileEntry] = {}
        digest = hashlib.sha256()
        for path in sorted(files):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            try:
                content = path.read_bytes()
            except OSError:
                continue
            lines = content.count(b"\n") + (
                1 if content and not content.endswith(b"\n") else 0)
            entries[relative] = FileEntry(
                relative, _file_digest(content), len(content), lines)
            digest.update(relative.encode("utf-8"))
            digest.update(content)
        version = 0
        status: dict[str, str] = {}
        if self.snapshot is not None:
            version = self.snapshot.version + 1
            status = dict(self.snapshot.status)
        return ProjectSnapshot(
            root.name or root.as_posix(), version, digest.hexdigest(),
            entries, status)

    def set_status(self, module: str, value: str, actor: str) -> None:
        if actor not in self.authorized_actors:
            raise PermissionError(
                f"actor {actor} is not authorized to update project status")
        if self.snapshot is None:
            raise ValueError("no project snapshot captured yet")
        self.snapshot.status[module] = value

    def delta_from(self, root: Path, files: list[Path]) -> ProjectDelta | None:
        captured = self.capture(root, files)
        if self.snapshot is None:
            self.snapshot = captured
            return None
        delta = self.snapshot.delta(captured)
        self.snapshot = captured
        return delta

    def restore(self, snapshot: ProjectSnapshot | None) -> None:
        self.snapshot = snapshot