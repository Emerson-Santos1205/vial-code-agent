from __future__ import annotations

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
        if allowed_paths is not None:
            changed = self.paths(patch)
            unexpected = changed - allowed_paths
            if unexpected:
                names = ", ".join(sorted(unexpected))
                raise PatchError(f"patch changes files outside selected context: {names}")
        self._check(patch)

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
