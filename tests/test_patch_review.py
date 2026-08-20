import tempfile
import unittest
from pathlib import Path

from vial_code_agent.patch_review import PatchReviewGate


class PatchReviewTests(unittest.TestCase):
    def test_gate_accepts_scoped_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.py").write_text("value = 1\n", encoding="utf-8")
            patch = "--- a/source.py\n+++ b/source.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
            review = PatchReviewGate(root).review(patch, {"source.py"})
            self.assertTrue(review.passed)
            self.assertTrue(review.checks["not_no_op"])

    def test_gate_rejects_test_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_source.py").write_text("value = 1\n", encoding="utf-8")
            patch = "--- a/test_source.py\n+++ b/test_source.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
            review = PatchReviewGate(root).review(
                patch, {"test_source.py"}, {"test_source.py"})
            self.assertFalse(review.passed)
            self.assertFalse(review.checks["no_test_changes"])
