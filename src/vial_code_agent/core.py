from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VialCoreReference:
    """Local reference to a VIAL checkout until the core is packaged."""

    root: Path

    def exists(self) -> bool:
        return self.root.is_dir()
