from __future__ import annotations

from pathlib import Path


def select_files(root: Path, includes: list[str], excludes: list[str]) -> list[Path]:
    """Return deterministic source selection without traversing excluded paths."""
    root = root.resolve()
    excluded = set(excludes)
    selected: set[Path] = set()
    for pattern in includes:
        for path in root.rglob(pattern):
            if not path.is_file():
                continue
            relative_parts = path.relative_to(root).parts
            if excluded.intersection(relative_parts):
                continue
            selected.add(path)
    return sorted(selected)
