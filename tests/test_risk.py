import unittest

from vial_code_agent.risk import RiskPolicy, classify_task


class RiskTests(unittest.TestCase):
    def test_classifies_dangerous_operations(self) -> None:
        self.assertEqual(classify_task("update dependency"), "high")
        self.assertEqual(classify_task("deploy to production"), "critical")
        self.assertEqual(classify_task("inspect this file"), "low")

    def test_auto_policy_caps_risk(self) -> None:
        policy = RiskPolicy("medium")
        self.assertTrue(policy.allows_auto("low"))
        self.assertTrue(policy.allows_auto("medium"))
        self.assertFalse(policy.allows_auto("high"))
        self.assertFalse(policy.allows_auto("critical"))
