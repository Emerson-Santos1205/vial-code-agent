from __future__ import annotations

import difflib
import subprocess
from pathlib import Path
from pathlib import PurePosixPath


class PatchError(ValueError):
    pass


class PatchApplier:
    """Validate and apply textual git patches inside one workspace."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _normalize(self, patch: str) -> str:
        if not patch.strip():
            raise PatchError("patch is empty")
        normalized_lines: list[str] = []
        in_hunk = False
        for line in patch.splitlines():
            if line.startswith("@@"):
                in_hunk = True
            elif line.startswith(("diff --git ", "--- ", "+++ ")):
                in_hunk = False
            if in_hunk and line == "":
                line = " "
            normalized_lines.append(line)
        return "\n".join(normalized_lines) + "\n"

    def paths(self, patch: str) -> set[str]:
        normalized = self._normalize(patch)
        result: set[str] = set()
        for line in normalized.splitlines():
            if line.startswith(("--- ", "+++ ")):
                path = line[4:].split("\t", 1)[0]
                if path != "/dev/null":
                    result.add(path.removeprefix("a/").removeprefix("b/"))
        return result

    def _check(self, patch: str, reverse: bool = False) -> tuple[list[str], str]:
        patch = self._normalize(patch)
        for line in patch.splitlines():
            if line.startswith(("--- ", "+++ ")):
                path = line[4:].split("\t", 1)[0]
                if path in ("/dev/null",):
                    continue
                relative = path.removeprefix("a/").removeprefix("b/")
                pure = PurePosixPath(relative)
                if pure.is_absolute() or ".." in pure.parts:
                    raise PatchError(f"patch path escapes workspace: {path}")
                if pure.parts and (pure.parts[0] == ".git"):
                    raise PatchError(f"patch cannot modify git metadata: {path}")
                candidate = (self.root / relative).resolve()
                if self.root not in candidate.parents and candidate != self.root:
                    raise PatchError(f"patch path escapes workspace: {path}")
                current = self.root
                for part in pure.parts:
                    current = current / part
                    if current.is_symlink():
                        raise PatchError(f"patch path traverses symlink: {path}")
        command = [
            "git", "apply", "--ignore-space-change", "--ignore-whitespace",
            "--recount",
            "--whitespace=nowarn" if reverse else "--whitespace=error",
        ]
        if reverse:
            command.append("--reverse")
        check = subprocess.run(
            command + ["--check", "-"], input=patch.encode("utf-8"), cwd=self.root,
            capture_output=True, check=False,
        )
        if check.returncode != 0:
            detail = check.stderr.decode("utf-8", errors="replace").strip()
            raise PatchError(detail or "patch validation failed")
        return command, patch

    def validate(self, patch: str, allowed_paths: set[str] | None = None) -> None:
        removed = [line[1:] for line in patch.splitlines()
                   if line.startswith("-") and not line.startswith("---")]
        added = [line[1:] for line in patch.splitlines()
                 if line.startswith("+") and not line.startswith("+++")]
        if removed == added and removed:
            raise PatchError("patch is a no-op: added and removed lines are identical")
        if allowed_paths is not None:
            changed = self.paths(patch)
            unexpected = changed - allowed_paths
            if unexpected:
                names = ", ".join(sorted(unexpected))
                raise PatchError(f"patch changes files outside selected context: {names}")
        self._check(patch)

    def repair_candidate(self, patch: str) -> str | None:
        """Repair one unambiguous malformed replacement in a staging tree.

        This never writes files. It derives a new diff only when one existing
        file has one unique removed block, so ambiguous model output remains a
        hard failure.
        """
        paths = self.paths(patch)
        if len(paths) != 1:
            return None
        removed = [line[1:] for line in patch.splitlines()
                   if line.startswith("-") and not line.startswith("---")]
        added = [line[1:] for line in patch.splitlines()
                 if line.startswith("+") and not line.startswith("+++")]
        if not added:
            return None
        relative = next(iter(paths))
        target = self.root / relative
        if not target.is_file() or target.is_symlink():
            return None
        original = target.read_text(encoding="utf-8").splitlines(keepends=True)
        new = [line if line.endswith("\n") else line + "\n" for line in added]
        if removed:
            old = [line if line.endswith("\n") else line + "\n" for line in removed]
            matches = [index for index in range(len(original) - len(old) + 1)
                       if original[index:index + len(old)] == old]
        else:
            code_lines = [line for line in new if "=" in line and
                          not line.lstrip().startswith("#")]
            if len(code_lines) != 1:
                return None
            prefix = code_lines[0].split("=", 1)[0].rstrip()
            matches = [index for index, line in enumerate(original)
                       if line.split("=", 1)[0].rstrip() == prefix]
            old = [original[matches[0]]] if len(matches) == 1 else []
        if len(matches) != 1:
            return self._repair_hunks(patch, relative, original)
        updated = original[:matches[0]] + new + original[matches[0] + len(old):]
        return "".join(difflib.unified_diff(
            original, updated, fromfile=f"a/{relative}", tofile=f"b/{relative}"))

    def _repair_hunks(self, patch: str, relative: str,
                      original: list[str]) -> str | None:
        """Rebuild uniquely locatable hunks while ignoring stale line numbers."""
        hunks: list[list[str]] = []
        current: list[str] | None = None
        for line in self._normalize(patch).splitlines():
            if line.startswith("@@"):
                current = []
                hunks.append(current)
            elif current is not None and line[:1] in (" ", "+", "-"):
                current.append(line)
        if not hunks:
            return None

        updated = list(original)
        for hunk in hunks:
            old_block = [line[1:] + "\n" for line in hunk if line.startswith((" ", "-"))]
            new_block = [line[1:] + "\n" for line in hunk if line.startswith((" ", "+"))]
            matches = [index for index in range(len(updated) - len(old_block) + 1)
                       if updated[index:index + len(old_block)] == old_block]
            if len(matches) != 1:
                return None
            index = matches[0]
            updated[index:index + len(old_block)] = new_block
        return "".join(difflib.unified_diff(
            original, updated, fromfile=f"a/{relative}", tofile=f"b/{relative}"))

    def _run(self, patch: str, reverse: bool = False) -> None:
        command, patch = self._check(patch, reverse)
        applied = subprocess.run(
            command + ["-"], input=patch.encode("utf-8"), cwd=self.root,
            capture_output=True, check=False,
        )
        if applied.returncode != 0:
            detail = applied.stderr.decode("utf-8", errors="replace").strip()
            raise PatchError(detail or "patch application failed")

    def apply(self, patch: str) -> None:
        self._run(patch)

    def reverse(self, patch: str) -> None:
        self._run(patch, reverse=True)
