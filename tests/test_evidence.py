from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.evidence import validate_candidate


PATCH = "--- a/solution.py\n+++ b/solution.py\n@@ -1 +1 @@\n-return 0\n+return 1\n"


class EvidenceTests(unittest.TestCase):
    def test_behavioral_evidence_runs_in_isolated_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "solution.py").write_text("return 0\n", encoding="utf-8")
            result = validate_candidate(
                root, PATCH, ["python", "-c", "from pathlib import Path; assert Path('solution.py').read_text() == 'return 1\\n'"])
            self.assertTrue(result.passed)
            self.assertEqual((root / "solution.py").read_text(), "return 0\n")

    def test_invalid_candidate_has_no_behavioral_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "solution.py").write_text("return 0\n", encoding="utf-8")
            result = validate_candidate(root, "not a patch")
            self.assertFalse(result.passed)
