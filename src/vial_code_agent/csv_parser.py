from __future__ import annotations


def parse_csv_line(line: str) -> list[str]:
    """Parse one CSV line with comma separators and double-quoted fields."""
    fields: list[str] = []
    field: list[str] = []
    in_quotes = False
    index = 0

    while index < len(line):
        char = line[index]

        if char == '"':
            if in_quotes and index + 1 < len(line) and line[index + 1] == '"':
                field.append('"')
                index += 1
            else:
                in_quotes = not in_quotes
        elif char == "," and not in_quotes:
            fields.append("".join(field))
            field = []
        else:
            field.append(char)

        index += 1

    fields.append("".join(field))
    return fields
