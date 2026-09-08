from __future__ import annotations

import unittest

from vial_code_agent.git_ops import (
    GitPolicy,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_HIGH,
    RISK_CRITICAL,
)


class GitPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = GitPolicy()

    def test_read_commands_are_low_risk(self) -> None:
        """Read-only commands should be low risk."""
        for cmd in ["status", "diff", "log", "show", "blame"]:
            result = self.policy.classify([cmd])
            self.assertEqual(result.category, "read")
            self.assertEqual(result.risk, RISK_LOW)
            self.assertFalse(result.requires_consensus)
            self.assertFalse(result.requires_approval)

    def test_local_mutation_commands_are_medium_risk(self) -> None:
        """Local mutation commands should be medium risk."""
        for cmd in ["add", "rm", "mv", "commit", "stash"]:
            result = self.policy.classify([cmd])
            self.assertEqual(result.category, "local_mutation")
            self.assertEqual(result.risk, RISK_MEDIUM)
            self.assertTrue(result.requires_consensus)
            self.assertFalse(result.requires_approval)

    def test_destructive_reset_hard(self) -> None:
        """git reset --hard should be destructive."""
        result = self.policy.classify(["reset", "--hard", "HEAD~1"])
        self.assertEqual(result.category, "destructive")
        self.assertEqual(result.risk, RISK_CRITICAL)
        self.assertTrue(result.requires_consensus)
        self.assertTrue(result.requires_approval)

    def test_destructive_clean_fd(self) -> None:
        """git clean -fd should be destructive."""
        result = self.policy.classify(["clean", "-fd"])
        self.assertEqual(result.category, "destructive")
        self.assertEqual(result.risk, RISK_CRITICAL)
        self.assertTrue(result.requires_approval)

    def test_destructive_checkout_force(self) -> None:
        """git checkout --force should be destructive."""
        result = self.policy.classify(["checkout", "--force", "main"])
        self.assertEqual(result.category, "destructive")
        self.assertEqual(result.risk, RISK_CRITICAL)
        self.assertTrue(result.requires_approval)

    def test_destructive_push_force(self) -> None:
        """git push --force should be destructive."""
        result = self.policy.classify(["push", "--force", "origin", "main"])
        self.assertEqual(result.category, "destructive")
        self.assertEqual(result.risk, RISK_CRITICAL)
        self.assertTrue(result.requires_approval)

    def test_remote_commands_are_high_risk(self) -> None:
        """Remote commands should be high risk."""
        for cmd in ["push", "pull", "fetch", "clone"]:
            result = self.policy.classify([cmd])
            self.assertEqual(result.category, "remote")
            self.assertEqual(result.risk, RISK_HIGH)
            self.assertTrue(result.requires_consensus)
            self.assertFalse(result.requires_approval)

    def test_configuration_commands_are_medium_risk(self) -> None:
        """Configuration commands should be medium risk."""
        result = self.policy.classify(["config"])
        self.assertEqual(result.category, "configuration")
        self.assertEqual(result.risk, RISK_MEDIUM)
        self.assertTrue(result.requires_consensus)

    def test_non_destructive_reset(self) -> None:
        """git reset without --hard should be local mutation."""
        result = self.policy.classify(["reset", "HEAD~1"])
        self.assertEqual(result.category, "local_mutation")
        self.assertEqual(result.risk, RISK_MEDIUM)

    def test_non_destructive_checkout(self) -> None:
        """git checkout without --force should be local mutation."""
        result = self.policy.classify(["checkout", "main"])
        self.assertEqual(result.category, "local_mutation")
        self.assertEqual(result.risk, RISK_MEDIUM)

    def test_empty_args(self) -> None:
        """Empty args should default to read."""
        result = self.policy.classify([])
        self.assertEqual(result.category, "read")
        self.assertEqual(result.risk, RISK_LOW)

    def test_branch_without_flags(self) -> None:
        """git branch (list) should be read."""
        result = self.policy.classify(["branch"])
        self.assertEqual(result.category, "read")
        self.assertEqual(result.risk, RISK_LOW)


if __name__ == "__main__":
    unittest.main()
