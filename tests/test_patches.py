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

    def test_reverses_applied_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "value.txt").write_text("old\n", encoding="utf-8")
            patch = """--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
+new
"""
            applier = PatchApplier(root)
            applier.apply(patch)
            applier.reverse(patch)
            self.assertEqual((root / "value.txt").read_text(encoding="utf-8"), "old\n")

    def test_rejects_symlink_to_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / f"{root.name}-outside.txt"
            outside.write_text("old\n", encoding="utf-8")
            link = root / "linked.txt"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            patch = """--- a/linked.txt
+++ b/linked.txt
@@ -1 +1 @@
-old
+new
"""
            with self.assertRaises(PatchError):
                PatchApplier(root).apply(patch)

    def test_accepts_missing_prefix_on_blank_context_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "value.txt").write_text("old\n\nkeep\n", encoding="utf-8")
            patch = """--- a/value.txt
+++ b/value.txt
@@ -1,3 +1,4 @@
 old

+new
 keep
"""
            PatchApplier(root).apply(patch)
            self.assertEqual((root / "value.txt").read_text(encoding="utf-8"), "old\n\nnew\nkeep\n")

    def test_recounts_inconsistent_hunk_header_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "value.txt").write_text("old\nkeep\n", encoding="utf-8")
            patch = """--- a/value.txt
+++ b/value.txt
@@ -1,8 +1,8 @@
 old
-keep
+new
"""
            PatchApplier(root).apply(patch)
            self.assertEqual((root / "value.txt").read_text(encoding="utf-8"), "old\nnew\n")

    def test_repairs_unambiguous_malformed_context_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "value.txt"
            source.write_text("before\nold\nafter\n", encoding="utf-8")
            malformed = """--- a/value.txt
+++ b/value.txt
@@ -99,5 +99,5 @@
 wrong context
-old
+new
"""
            repaired = PatchApplier(root).repair_candidate(malformed)
            self.assertIsNotNone(repaired)
            self.assertEqual(source.read_text(encoding="utf-8"), "before\nold\nafter\n")
            PatchApplier(root).apply(repaired or "")
            self.assertEqual(source.read_text(encoding="utf-8"), "before\nnew\nafter\n")

    def test_repairs_multiple_hunks_with_stale_line_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "value.txt"
            source.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
            malformed = """--- a/value.txt
+++ b/value.txt
@@ -90,1 +90,1 @@
-one
+ONE
@@ -190,1 +190,1 @@
-four
+FOUR
"""
            repaired = PatchApplier(root).repair_candidate(malformed)
            self.assertIsNotNone(repaired)
            PatchApplier(root).apply(repaired or "")
            self.assertEqual(source.read_text(encoding="utf-8"), "ONE\ntwo\nthree\nFOUR\n")
