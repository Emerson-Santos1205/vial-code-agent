from __future__ import annotations


def format_bytes(value: int) -> str:
    """Converte bytes para uma unidade decimal legível com duas casas decimais."""
    units = ("B", "KB", "MB", "GB")
    size = float(value)
    unit_index = 0

    while size >= 1000 and unit_index < len(units) - 1:
        size /= 1000
        unit_index += 1

    return f"{size:.2f} {units[unit_index]}"
