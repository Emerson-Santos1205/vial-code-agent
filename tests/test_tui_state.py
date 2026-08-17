import unittest

from vial_code_agent.tui_state import TUIState


class TUIStateTests(unittest.TestCase):
    def test_pipeline_is_presentation_only_and_ordered(self) -> None:
        state = TUIState()
        state.start("fix the bug")
        state.advance("CONSENSUS")
        stages = state.pipeline()
        self.assertEqual(stages[0], ("TASK", "done"))
        self.assertEqual(stages[4], ("CONSENSUS", "running"))
        self.assertEqual(stages[5], ("AUTHORIZATION", "pending"))

    def test_failure_state_is_visible(self) -> None:
        state = TUIState()
        state.start("generate patch")
        state.advance("PATCH")
        state.failure_type = "PATCH_CONTRACT"
        state.finish(False, "patch generation failed")
        self.assertEqual(state.status, "FAILED")
        self.assertIn("patch generation failed", state.events)
