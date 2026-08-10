from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.workspace import select_files


class WorkspaceTests(unittest.TestCase):
    def test_selection_is_sorted_and_excludes_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "b.py").write_text("", encoding="utf-8")
            (root / "a.py").write_text("", encoding="utf-8")
            hidden = root / ".git"
            hidden.mkdir()
            (hidden / "ignored.py").write_text("", encoding="utf-8")

            result = select_files(root, ["*.py"], [".git"])

            self.assertEqual([path.name for path in result], ["a.py", "b.py"])
