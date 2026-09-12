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

    def test_patch_contract_recovery_is_opt_in(self) -> None:
        provider = Mock()
        diff = "--- a/source.py\n+++ b/source.py\n@@ -1 +1 @@\n-old\n+new\n"
        provider.generate.side_effect = [
            ModelResponse("Here is an explanation", 0),
            ModelResponse(diff, 0),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("old\n", encoding="utf-8")
            result = CodeAgent(provider).generate(
                "change it", root, [source], max_attempts=2)
            self.assertEqual(result.attempts, 2)
            self.assertIsNotNone(result.patch)
            self.assertEqual(provider.generate.call_count, 2)

    def test_patch_contract_does_not_retry_by_default(self) -> None:
        provider = Mock()
        provider.generate.return_value = ModelResponse("not a patch", 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("old\n", encoding="utf-8")
            result = CodeAgent(provider).generate("change it", root, [source])
        self.assertEqual(result.attempts, 1)
        self.assertIsNone(result.patch)
        self.assertEqual(provider.generate.call_count, 1)

    def test_deterministic_first_without_runtime_never_calls_model(self) -> None:
        provider = Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("x = 1  \n", encoding="utf-8")
            result = CodeAgent(provider).generate(
                "trim trailing whitespace", root, [source])
            provider.generate.assert_not_called()
            self.assertEqual(result.route, "deterministic")
            self.assertIsNotNone(result.patch)
            self.assertIn("-x = 1  \n+x = 1\n", result.patch)

    def test_workspace_changed_detected_with_extracted_patch(self) -> None:
        diff = "--- a/source.py\n+++ b/source.py\n@@ -1 +1 @@\n-old\n+new\n"
        provider = Mock()
        provider.generate.return_value = ModelResponse(diff, 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("old\n", encoding="utf-8")
            result = CodeAgent(provider).generate("change it", root, [source])
            self.assertIsNotNone(result.patch)
            self.assertFalse(result.workspace_changed)

            source.write_text("old\n", encoding="utf-8")

            def mutate_and_return(task, directory=None, files=None, **kwargs):
                source.write_text("new\n", encoding="utf-8")
                return ModelResponse(diff, 0)

            provider.generate.side_effect = mutate_and_return
            result = CodeAgent(provider).generate("change it", root, [source])
            self.assertIsNotNone(result.patch)
            self.assertTrue(result.workspace_changed)

    def test_provider_writes_are_confined_to_staging_workspace(self) -> None:
        provider = Mock()
        diff = "--- a/source.py\n+++ b/source.py\n@@ -1 +1 @@\n-old\n+new\n"

        def mutate_staging(task, directory=None, files=None, **kwargs):
            files[0].write_text("provider change\n", encoding="utf-8")
            return ModelResponse(diff, 0)

        provider.generate.side_effect = mutate_staging
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("old\n", encoding="utf-8")
            result = CodeAgent(provider).generate("change it", root, [source])
            self.assertFalse(result.workspace_changed)
            self.assertEqual(source.read_text(encoding="utf-8"), "old\n")

    def test_search_replace_format_converts_to_unified_diff(self) -> None:
        sr_text = (
            "### source.py\n"
            "<<<<<<< SEARCH\n"
            "old\n"
            "=======\n"
            "new\n"
            ">>>>>>> REPLACE\n"
        )
        provider = Mock()
        provider.generate.return_value = ModelResponse(sr_text, 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("old\n", encoding="utf-8")
            result = CodeAgent(provider).generate(
                "change it", root, [source], edit_format="search-replace",
                max_attempts=2)
            self.assertIsNotNone(result.patch)
            self.assertIn("diff --git", result.patch)
            self.assertIn("+++ b/source.py", result.patch)

    def test_search_replace_format_is_parsed_after_retry(self) -> None:
        sr_text = (
            "### source.py\n"
            "<<<<<<< SEARCH\n"
            "old\n"
            "=======\n"
            "new\n"
            ">>>>>>> REPLACE\n"
        )
        provider = Mock()
        provider.generate.side_effect = [
            ModelResponse("not a patch", 0),
            ModelResponse(sr_text, 0),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("old\n", encoding="utf-8")
            result = CodeAgent(provider).generate(
                "change it", root, [source], edit_format="search-replace",
                max_attempts=2)
            self.assertEqual(result.attempts, 2)
            self.assertIsNotNone(result.patch)

    def test_search_replace_format_passes_through_unified_diff(self) -> None:
        diff = "--- a/source.py\n+++ b/source.py\n@@ -1 +1 @@\n-old\n+new\n"
        provider = Mock()
        provider.generate.return_value = ModelResponse(diff, 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("old\n", encoding="utf-8")
            result = CodeAgent(provider).generate(
                "change it", root, [source], edit_format="search-replace")
            self.assertIsNotNone(result.patch)
            self.assertIn("+++ b/source.py", result.patch)
            self.assertIn("-old", result.patch)
            self.assertIn("+new", result.patch)

    def test_search_replace_format_prompt_contains_search_replace(self) -> None:
        provider = Mock()
        provider.generate.return_value = ModelResponse(
            "--- a/source.py\n+++ b/source.py\n@@ -1 +1 @@\n-old\n+new\n", 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("old\n", encoding="utf-8")
            CodeAgent(provider).generate(
                "change it", root, [source], edit_format="search-replace")
            call_args = provider.generate.call_args
            prompt = call_args[0][0]
            self.assertIn("SEARCH/REPLACE blocks", prompt)
            self.assertIn("<<<<<<< SEARCH", prompt)

    def test_unified_diff_format_prompt_contains_unified_diff(self) -> None:
        provider = Mock()
        provider.generate.return_value = ModelResponse(
            "--- a/source.py\n+++ b/source.py\n@@ -1 +1 @@\n-old\n+new\n", 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.py"
            source.write_text("old\n", encoding="utf-8")
            CodeAgent(provider).generate(
                "change it", root, [source], edit_format="unified-diff")
            call_args = provider.generate.call_args
            prompt = call_args[0][0]
            self.assertIn("unified diff", prompt)
            self.assertNotIn("<<<<<<< SEARCH", prompt)
