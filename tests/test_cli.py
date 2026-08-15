from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vial_code_agent.agent import GenerationResult
from vial_code_agent.cli import main
from vial_code_agent.core import VialCoreReference
from vial_code_agent.model import ModelResponse
from vial_code_agent.vial_runtime import VialRuntime


PATCH = """--- a/source.txt
+++ b/source.txt
@@ -1 +1 @@
-old
+new
"""

VENDOR = Path(__file__).resolve().parents[1] / "vendor" / "vial-core"


def _runtime(root: Path) -> VialRuntime:
    return VialRuntime(VialCoreReference(VENDOR), root / ".vial-state")


class CliIntegrationTests(unittest.TestCase):
    def test_review_validates_and_prints_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("old\n", encoding="utf-8")
            patch_file = root / "change.patch"
            patch_file.write_text(PATCH, encoding="utf-8")

            result = main(["--review", str(patch_file), "--root", str(root)])

            self.assertEqual(result, 0)

    def test_fake_provider_patch_is_applied_without_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("old\n", encoding="utf-8")
            generated = GenerationResult(ModelResponse("", 0), PATCH)

            class FakeAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            with patch("vial_code_agent.cli.CodeAgent", FakeAgent):
                result = main(
                    [
                        "--fix", "change old to new", "--root", str(root),
                        "--include", "source.txt",
                        "--test-command", sys.executable, "-c",
                        "import pathlib; assert pathlib.Path('source.txt').read_text() == 'new\\n'",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(source.read_text(encoding="utf-8"), "new\n")

    def test_failed_verification_rolls_back_and_records_compensation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("old\n", encoding="utf-8")
            generated = GenerationResult(ModelResponse("", 0), PATCH)

            class FakeAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            with patch("vial_code_agent.cli.CodeAgent", FakeAgent):
                result = main(
                    [
                        "--fix", "change old to new", "--root", str(root),
                        "--vial-root", str(VENDOR),
                        "--include", "source.txt",
                        "--test-command", sys.executable, "-c", "import sys; sys.exit(1)",
                    ]
                )

            self.assertEqual(result, 1)
            self.assertEqual(source.read_text(encoding="utf-8"), "old\n")
            runtime = _runtime(root)
            op_id = hashlib.sha256(PATCH.encode("utf-8")).hexdigest()
            self.assertIn(
                "ROLLBACK-" + op_id,
                [intent.operation_id for intent in runtime.coordinator.intents.values()],
            )

    def test_keep_on_failure_preserves_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("old\n", encoding="utf-8")
            generated = GenerationResult(ModelResponse("", 0), PATCH)

            class FakeAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            with patch("vial_code_agent.cli.CodeAgent", FakeAgent):
                result = main(
                    [
                        "--fix", "change old to new", "--root", str(root),
                        "--vial-root", str(VENDOR),
                        "--include", "source.txt",
                        "--keep-on-failure",
                        "--test-command", sys.executable, "-c", "import sys; sys.exit(1)",
                    ]
                )

            self.assertEqual(result, 1)
            self.assertEqual(source.read_text(encoding="utf-8"), "new\n")

    def test_trace_requires_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = main(["--trace", "DEC-0001", "--root", directory])
            self.assertEqual(result, 2)
