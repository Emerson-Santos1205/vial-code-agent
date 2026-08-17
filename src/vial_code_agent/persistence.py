"""Transactional persistence owned by the application runtime."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
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
                (generation / name).write_bytes(payload)
                manifest[name] = hashlib.sha256(payload).hexdigest()
            (generation / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            pointer = self.root / "current-snapshot.json.tmp"
            pointer.write_text(json.dumps({"generation": generation.name}) + "\n", encoding="utf-8")
            with pointer.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(pointer, self.root / "current-snapshot.json")
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
            generation = self.root / "snapshots" / json.loads(
                pointer.read_text(encoding="utf-8"))["generation"]
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
