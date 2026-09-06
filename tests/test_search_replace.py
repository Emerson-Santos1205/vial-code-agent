from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.patches import PatchApplier, PatchError
from vial_code_agent.search_replace import parse_search_replace


class SearchReplaceTests(unittest.TestCase):

    def test_single_block_single_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "value.txt").write_text("old\n", encoding="utf-8")
            text = (
                "### value.txt\n"
                "<<<<<<< SEARCH\n"
                "old\n"
                "=======\n"
                "new\n"
                ">>>>>>> REPLACE\n"
            )
            diff = parse_search_replace(text, root)
            self.assertIn("--- a/value.txt", diff)
            self.assertIn("+++ b/value.txt", diff)
            PatchApplier(root).apply(diff)
            self.assertEqual((root / "value.txt").read_text(encoding="utf-8"), "new\n")

    def test_multiple_blocks_same_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "code.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
            text = (
                "### code.py\n"
                "<<<<<<< SEARCH\n"
                "x = 1\n"
                "=======\n"
                "x = 10\n"
                ">>>>>>> REPLACE\n"
                "<<<<<<< SEARCH\n"
                "y = 2\n"
                "=======\n"
                "y = 20\n"
                ">>>>>>> REPLACE\n"
            )
            diff = parse_search_replace(text, root)
            PatchApplier(root).apply(diff)
            self.assertEqual(
                (root / "code.py").read_text(encoding="utf-8"), "x = 10\ny = 20\n")

    def test_multiple_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("alpha\n", encoding="utf-8")
            (root / "b.txt").write_text("beta\n", encoding="utf-8")
            text = (
                "### a.txt\n"
                "<<<<<<< SEARCH\n"
                "alpha\n"
                "=======\n"
                "ALPHA\n"
                ">>>>>>> REPLACE\n"
                "### b.txt\n"
                "<<<<<<< SEARCH\n"
                "beta\n"
                "=======\n"
                "BETA\n"
                ">>>>>>> REPLACE\n"
            )
            diff = parse_search_replace(text, root)
            PatchApplier(root).apply(diff)
            self.assertEqual((root / "a.txt").read_text(encoding="utf-8"), "ALPHA\n")
            self.assertEqual((root / "b.txt").read_text(encoding="utf-8"), "BETA\n")

    def test_create_new_file_empty_search(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = (
                "### new_file.py\n"
                "<<<<<<< SEARCH\n"
                "=======\n"
                "print('hello')\n"
                ">>>>>>> REPLACE\n"
            )
            diff = parse_search_replace(text, root)
            self.assertIn("+++ b/new_file.py", diff)
            self.assertIn("+print('hello')", diff)
            self.assertIn("--- /dev/null", diff)

    def test_ambiguous_search_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dup.txt").write_text("line\nsame\nline\n", encoding="utf-8")
            text = (
                "### dup.txt\n"
                "<<<<<<< SEARCH\n"
                "line\n"
                "=======\n"
                "LINE\n"
                ">>>>>>> REPLACE\n"
            )
            with self.assertRaisesRegex(PatchError, "ambiguous"):
                parse_search_replace(text, root)

    def test_search_not_found_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "file.txt").write_text("actual\n", encoding="utf-8")
            text = (
                "### file.txt\n"
                "<<<<<<< SEARCH\n"
                "nonexistent\n"
                "=======\n"
                "replacement\n"
                ">>>>>>> REPLACE\n"
            )
            with self.assertRaisesRegex(PatchError, "not found"):
                parse_search_replace(text, root)

    def test_no_path_header_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            text = (
                "<<<<<<< SEARCH\n"
                "old\n"
                "=======\n"
                "new\n"
                ">>>>>>> REPLACE\n"
            )
            with self.assertRaisesRegex(PatchError, "no ###"):
                parse_search_replace(text, Path(directory))

    def test_no_sr_blocks_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            text = "### file.txt\nsome unrelated content\n"
            with self.assertRaisesRegex(PatchError, "no SEARCH/REPLACE"):
                parse_search_replace(text, Path(directory))

    def test_escape_path_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = (
                "### ../outside.txt\n"
                "<<<<<<< SEARCH\n"
                "=======\n"
                "evil\n"
                ">>>>>>> REPLACE\n"
            )
            with self.assertRaisesRegex(PatchError, "escapes workspace"):
                parse_search_replace(text, root)

    def test_symlink_traversal_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / f"{root.name}-outside.txt"
            outside.write_text("secret\n", encoding="utf-8")
            link = root / "linked.txt"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            text = (
                "### linked.txt\n"
                "<<<<<<< SEARCH\n"
                "secret\n"
                "=======\n"
                "REDACTED\n"
                ">>>>>>> REPLACE\n"
            )
            with self.assertRaisesRegex(PatchError, "symlink"):
                parse_search_replace(text, root)

    def test_git_metadata_path_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = (
                "### .git/config\n"
                "<<<<<<< SEARCH\n"
                "=======\n"
                "evil\n"
                ">>>>>>> REPLACE\n"
            )
            with self.assertRaisesRegex(PatchError, "git metadata"):
                parse_search_replace(text, root)

    def test_empty_search_on_existing_file_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "exists.txt").write_text("content\n", encoding="utf-8")
            text = (
                "### exists.txt\n"
                "<<<<<<< SEARCH\n"
                "=======\n"
                "new content\n"
                ">>>>>>> REPLACE\n"
            )
            with self.assertRaisesRegex(PatchError, "already exists"):
                parse_search_replace(text, root)

    def test_multiline_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "func.py").write_text(
                "def foo():\n    pass\n", encoding="utf-8")
            text = (
                "### func.py\n"
                "<<<<<<< SEARCH\n"
                "def foo():\n"
                "    pass\n"
                "=======\n"
                "def foo():\n"
                "    return 42\n"
                ">>>>>>> REPLACE\n"
            )
            diff = parse_search_replace(text, root)
            PatchApplier(root).apply(diff)
            self.assertEqual(
                (root / "func.py").read_text(encoding="utf-8"),
                "def foo():\n    return 42\n")

    def test_diff_output_is_canonical_unified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "x.txt").write_text("hello\nworld\n", encoding="utf-8")
            text = (
                "### x.txt\n"
                "<<<<<<< SEARCH\n"
                "world\n"
                "=======\n"
                "WORLD\n"
                ">>>>>>> REPLACE\n"
            )
            diff = parse_search_replace(text, root)
            self.assertTrue(diff.startswith("diff --git a/x.txt b/x.txt\n"))
            self.assertIn("--- a/x.txt\n+++ b/x.txt\n", diff)
            self.assertIn("@@ ", diff)
