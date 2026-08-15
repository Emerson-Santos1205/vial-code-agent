from __future__ import annotations


def bytes_to_decimal_unit(num_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB")
    size = float(num_bytes)
    unit_index = 0

    while size >= 1000 and unit_index < len(units) - 1:
        size /= 1000
        unit_index += 1

    return f"{size:.2f} {units[unit_index]}"
