from __future__ import annotations

import subprocess
from pathlib import Path


class PatchError(ValueError):
    pass


class PatchApplier:
    """Validate and apply textual git patches inside one workspace."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _run(self, patch: str, reverse: bool = False) -> None:
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
        patch = "\n".join(normalized_lines) + "\n"
        for line in patch.splitlines():
            if line.startswith(("--- ", "+++ ")):
                path = line[4:].split("\t", 1)[0]
                if path in ("/dev/null",):
                    continue
                relative = path.removeprefix("a/").removeprefix("b/")
                candidate = (self.root / relative).resolve()
                if self.root not in candidate.parents and candidate != self.root:
                    raise PatchError(f"patch path escapes workspace: {path}")
        command = [
            "git", "apply", "--recount", "--ignore-space-change", "--ignore-whitespace",
            "--whitespace=nowarn" if reverse else "--whitespace=error",
        ]
        if reverse:
            command.append("--reverse")
        check = subprocess.run(
            command + ["--check", "-"], input=patch, text=True, cwd=self.root,
            capture_output=True, check=False,
        )
        if check.returncode != 0:
            raise PatchError(check.stderr.strip() or "patch validation failed")
        applied = subprocess.run(
            command + ["-"], input=patch, text=True, cwd=self.root,
            capture_output=True, check=False,
        )
        if applied.returncode != 0:
            raise PatchError(applied.stderr.strip() or "patch application failed")

    def apply(self, patch: str) -> None:
        self._run(patch)

    def reverse(self, patch: str) -> None:
        self._run(patch, reverse=True)
