from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from vial_code_agent.agent import CodeAgent, build_prompt
from vial_code_agent.model import ModelResponse


class AgentTests(unittest.TestCase):
    def test_prompt_contains_relative_files_and_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("return 42", encoding="utf-8")
            prompt = build_prompt("fix it", root, [source], max_chars=30)
            self.assertIn("Task: fix it", prompt)
            self.assertIn("Context truncated", prompt)

    def test_extracts_patch_from_provider_response(self) -> None:
        provider = Mock()
        provider.generate.return_value = ModelResponse(
            "--- a/source.py\n+++ b/source.py\n@@ -1 +1 @@\n-old\n+new\n", 0
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("old\n", encoding="utf-8")
            result = CodeAgent(provider).generate("change it", root, [source])
            self.assertIsNotNone(result.patch)
