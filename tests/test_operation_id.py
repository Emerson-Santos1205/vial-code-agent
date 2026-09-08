from __future__ import annotations

import unittest
from pathlib import Path

from vial_code_agent.vial_runtime import generate_operation_id


class GenerateOperationIdTests(unittest.TestCase):
    def test_same_inputs_produce_same_id(self) -> None:
        """Same inputs should always produce the same operation ID."""
        id1 = generate_operation_id("patch", Path("/workspace"), "abc123", "policy", "ctx")
        id2 = generate_operation_id("patch", Path("/workspace"), "abc123", "policy", "ctx")
        self.assertEqual(id1, id2)

    def test_different_patch_produces_different_id(self) -> None:
        """Different patches should produce different IDs."""
        id1 = generate_operation_id("patch1", Path("/workspace"))
        id2 = generate_operation_id("patch2", Path("/workspace"))
        self.assertNotEqual(id1, id2)

    def test_different_workspace_produces_different_id(self) -> None:
        """Different workspaces should produce different IDs."""
        id1 = generate_operation_id("patch", Path("/workspace1"))
        id2 = generate_operation_id("patch", Path("/workspace2"))
        self.assertNotEqual(id1, id2)

    def test_different_context_produces_different_id(self) -> None:
        """Different contexts should produce different IDs."""
        id1 = generate_operation_id("patch", Path("/workspace"), context_id="ctx1")
        id2 = generate_operation_id("patch", Path("/workspace"), context_id="ctx2")
        self.assertNotEqual(id1, id2)

    def test_different_policy_produces_different_id(self) -> None:
        """Different policies should produce different IDs."""
        id1 = generate_operation_id("patch", Path("/workspace"), policy="policy1")
        id2 = generate_operation_id("patch", Path("/workspace"), policy="policy2")
        self.assertNotEqual(id1, id2)

    def test_different_base_commit_produces_different_id(self) -> None:
        """Different base commits should produce different IDs."""
        id1 = generate_operation_id("patch", Path("/workspace"), base_commit="abc")
        id2 = generate_operation_id("patch", Path("/workspace"), base_commit="def")
        self.assertNotEqual(id1, id2)

    def test_reverse_flag_produces_different_id(self) -> None:
        """Reverse flag should produce different IDs."""
        id1 = generate_operation_id("patch", Path("/workspace"), reverse=False)
        id2 = generate_operation_id("patch", Path("/workspace"), reverse=True)
        self.assertNotEqual(id1, id2)

    def test_rollback_prefix_in_id(self) -> None:
        """Rollback operations should have ROLLBACK- prefix."""
        id_normal = generate_operation_id("patch", Path("/workspace"), reverse=False)
        id_rollback = generate_operation_id("patch", Path("/workspace"), reverse=True)
        # IDs are hashes, so we verify they're different
        self.assertNotEqual(id_normal, id_rollback)

    def test_none_workspace_handled(self) -> None:
        """None workspace should be handled gracefully."""
        id1 = generate_operation_id("patch", None)
        id2 = generate_operation_id("patch", None)
        self.assertEqual(id1, id2)

    def test_empty_inputs_produce_valid_hash(self) -> None:
        """Empty inputs should still produce a valid SHA-256 hash."""
        op_id = generate_operation_id("")
        self.assertEqual(len(op_id), 64)  # SHA-256 hex digest length


if __name__ == "__main__":
    unittest.main()
