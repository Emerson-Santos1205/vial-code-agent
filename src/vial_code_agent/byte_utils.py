from __future__ import annotations

UNITS: tuple[tuple[str, int], ...] = (
    ("GB", 1_000_000_000),
    ("MB", 1_000_000),
    ("KB", 1_000),
)


def format_bytes_decimal(size: int | float) -> str:
    """Format a size in bytes to a human-readable decimal unit (KB/MB/GB).

    Uses the SI/decimal standard (1 KB = 1000 bytes) and two decimal places.
    """
    for unit, factor in (("GB", 1_000_000_000), ("MB", 1_000_000), ("KB", 1_000)):
        if size >= factor:
            return f"{size / factor:.2f} {unit}"
    return f"{size:.2f} B"
