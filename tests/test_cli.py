from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vial_code_agent.agent import GenerationResult
from vial_code_agent.cli import main
from vial_code_agent.model import ModelResponse


PATCH = """--- a/source.txt
+++ b/source.txt
@@ -1 +1 @@
-old
+new
"""


class CliIntegrationTests(unittest.TestCase):
    def test_review_validates_and_prints_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("old\n", encoding="utf-8")
            patch_file = root / "change.patch"
            patch_file.write_text(PATCH, encoding="utf-8")

            result = main(["review", str(patch_file), "--root", str(root)])

            self.assertEqual(result, 0)

    def test_fake_provider_patch_is_applied_without_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("old\n", encoding="utf-8")
            generated = GenerationResult(ModelResponse("", 0), PATCH)

            class FakeAgent:
                def __init__(self, provider: object) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            with patch("vial_code_agent.cli.CodeAgent", FakeAgent):
                result = main(
                    [
                        "--root", str(root), "--include", "source.txt", "--generate", "--apply", "--yes",
                        "--test-command", sys.executable, "-c", "import pathlib; assert pathlib.Path('source.txt').read_text() == 'new\\n'",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(source.read_text(encoding="utf-8"), "new\n")
