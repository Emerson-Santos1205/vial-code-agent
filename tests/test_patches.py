from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.patches import PatchApplier, PatchError


class PatchTests(unittest.TestCase):
    def test_applies_valid_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "value.txt").write_text("old\n", encoding="utf-8")
            patch = """--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
+new
"""

            PatchApplier(root).apply(patch)

            self.assertEqual((root / "value.txt").read_text(encoding="utf-8"), "new\n")

    def test_rejects_escape_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            patch = """--- a/../outside.txt
+++ b/../outside.txt
@@ -1 +1 @@
-old
+new
"""
            with self.assertRaises(PatchError):
                PatchApplier(Path(directory)).apply(patch)
