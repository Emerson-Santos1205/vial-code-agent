from __future__ import annotations

import difflib
from pathlib import Path, PurePosixPath

from .patches import PatchError

_SEARCH_MARKER = "<<<<<<< SEARCH"
_DIVIDER = "======="
_REPLACE_MARKER = ">>>>>>> REPLACE"


def _validate_path(relative: str, root: Path) -> None:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise PatchError(f"patch path escapes workspace: {relative}")
    if pure.parts and pure.parts[0] == ".git":
        raise PatchError(f"patch cannot modify git metadata: {relative}")
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise PatchError(f"patch path traverses symlink: {relative}")
    candidate = (root / relative).resolve()
    if root not in candidate.parents and candidate != root:
        raise PatchError(f"patch path escapes workspace: {relative}")


def _locate_block(content: str, search: str) -> list[int]:
    if not search:
        return []
    content_lines = content.splitlines(keepends=True)
    search_lines = search.splitlines(keepends=True)
    matches: list[int] = []
    for i in range(len(content_lines) - len(search_lines) + 1):
        if content_lines[i:i + len(search_lines)] == search_lines:
            matches.append(i)
    return matches


def _parse_file_blocks(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    current_path: str | None = None
    current_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("### "):
            if current_path is not None:
                blocks.append((current_path, "".join(current_lines)))
            current_path = line[4:].strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_path is not None:
        blocks.append((current_path, "".join(current_lines)))
    return blocks


def _parse_sr_pairs(content: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    current_lines: list[str] | None = None
    search_lines: list[str] = []
    for line in content.splitlines(keepends=True):
        if line.strip() == _SEARCH_MARKER:
            current_lines = search_lines = []
        elif line.strip() == _DIVIDER:
            current_lines = []
        elif line.strip() == _REPLACE_MARKER:
            if search_lines is not None and current_lines is not None:
                pairs.append(("".join(search_lines), "".join(current_lines)))
            current_lines = None
        elif current_lines is not None:
            current_lines.append(line)
        elif search_lines is not None:
            search_lines.append(line)
    return pairs


def parse_search_replace(text: str, root: Path) -> str:
    root = root.resolve()
    if not root.is_dir():
        raise PatchError(f"workspace root is not a directory: {root}")

    file_blocks = _parse_file_blocks(text)
    if not file_blocks:
        raise PatchError("no ### <path> header found in SEARCH/REPLACE text")

    diffs: list[str] = []
    for relative, content in file_blocks:
        _validate_path(relative, root)
        pairs = _parse_sr_pairs(content)
        if not pairs:
            raise PatchError(
                f"no SEARCH/REPLACE blocks found for file: {relative}")

        target = root / relative
        if target.is_file():
            original = target.read_text(encoding="utf-8")
        else:
            original = ""

        updated = original
        for search, replace in pairs:
            if search:
                matches = _locate_block(updated, search)
                if len(matches) == 0:
                    raise PatchError(
                        f"SEARCH block not found in {relative}")
                if len(matches) > 1:
                    raise PatchError(
                        f"SEARCH block matches {len(matches)} locations in "
                        f"{relative}, ambiguous")
                idx = matches[0]
                search_lines_count = len(search.splitlines())
                original_lines = updated.splitlines(keepends=True)
                replace_lines = replace.splitlines(keepends=True)
                updated_lines = (
                    original_lines[:idx]
                    + replace_lines
                    + original_lines[idx + search_lines_count:]
                )
                updated = "".join(updated_lines)
            else:
                if target.is_file():
                    raise PatchError(
                        f"cannot create {relative}: file already exists "
                        "(empty SEARCH block is for new files only)")
                replace_lines = replace.splitlines(keepends=True)
                updated = "".join(replace_lines)

        target = root / relative
        is_new = not target.is_file()
        fromfile = "/dev/null" if is_new else f"a/{relative}"
        tofile = f"b/{relative}"
        header = f"diff --git {fromfile} {tofile}\n"
        file_diff = header + "".join(difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        ))
        diffs.append(file_diff)

    return "".join(diffs)
