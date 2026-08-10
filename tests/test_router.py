from __future__ import annotations

import unittest

from vial_code_agent.router import ModelRouter


class ModelRouterTests(unittest.TestCase):
    def test_auto_routes_explanations_to_fast_model(self) -> None:
        self.assertEqual(ModelRouter().route("explain this module"), "fast")

    def test_auto_routes_changes_to_reasoning_model(self) -> None:
        self.assertEqual(ModelRouter().route("implement persistence"), "reasoning")

    def test_explicit_model_wins(self) -> None:
        self.assertEqual(ModelRouter().route("implement persistence", "local"), "local")
