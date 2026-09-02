"""Transactional persistence owned by the application runtime."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any


class TransactionalJsonRepository:
    """Wrap the VIAL repository with generation + checksum publication."""

    def __init__(self, root: Path, legacy: Any) -> None:
        self.root = Path(root)
        self.legacy = legacy

    def save(self, name: str, value: Any) -> Path:
        return self.legacy.save(name, value)

    def load(self, name: str) -> Any:
        return self.legacy.load(name)

    def save_snapshot(self, records: dict[str, Any]) -> Path:
        generation = self.root / "snapshots" / uuid.uuid4().hex
        generation.mkdir(parents=True, exist_ok=False)
        manifest: dict[str, str] = {}
        try:
            for name, value in records.items():
                if not name.endswith(".json") or Path(name).name != name:
                    raise ValueError(f"invalid record name: {name}")
                payload = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
                destination = generation / name
                with destination.open("wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                manifest[name] = hashlib.sha256(payload).hexdigest()
            manifest_path = generation / "manifest.json"
            with manifest_path.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._fsync_directory(generation)
            pointer = self.root / "current-snapshot.json.tmp"
            with pointer.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps({"generation": generation.name}) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pointer, self.root / "current-snapshot.json")
            self._fsync_directory(self.root)
            return generation
        except Exception:
            for child in generation.glob("*"):
                child.unlink()
            generation.rmdir()
            raise

    def load_snapshot(self) -> dict[str, Any] | None:
        pointer = self.root / "current-snapshot.json"
        if not pointer.is_file():
            return None
        try:
            generation_name = json.loads(pointer.read_text(encoding="utf-8"))["generation"]
            generation = (self.root / "snapshots" / generation_name).resolve()
            snapshots_root = (self.root / "snapshots").resolve()
            if snapshots_root not in generation.parents:
                raise ValueError("snapshot pointer escapes state directory")
            manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
            result = {}
            for name, expected in manifest.items():
                payload = (generation / name).read_bytes()
                if hashlib.sha256(payload).hexdigest() != expected:
                    raise ValueError(f"checksum mismatch: {name}")
                result[name] = json.loads(payload.decode("utf-8"))
            return result
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("active runtime snapshot is invalid") from exc

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            # Windows does not expose directory fsync; file and pointer fsync
            # still provide the strongest portable guarantee available.
            return
