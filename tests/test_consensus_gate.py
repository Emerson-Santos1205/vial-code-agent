from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.core import VialCoreReference
from vial_code_agent.patches import PatchApplier
from vial_code_agent.vial_runtime import PATCH_TOOL_ID, RISK_HIGH, VialRuntime


def _reference() -> VialCoreReference:
    root = Path(__file__).resolve().parents[1]
    reference = VialCoreReference(root / "vendor" / "vial-core")
    if not reference.exists():
        raise unittest.SkipTest("VIAL submodule is not initialized")
    return reference


def _runtime(state: Path) -> VialRuntime:
    return VialRuntime(_reference(), state)


PATCH = """--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
+new
"""


class ConsensusGateTests(unittest.TestCase):
    def test_read_only_tool_never_requires_consensus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.py"
            source.write_text("x = 1\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            runtime.set_workspace_root(root)
            tool = runtime.tools.get("TOOL-READ-FILE")
            self.assertFalse(runtime.requires_consensus(tool))
            result = runtime.invoke_tool(
                "TOOL-READ-FILE", {"path": "a.py"}, objective="read a.py")
            self.assertEqual(result.status, "SUCCESS")

    def test_mutation_tool_requires_consensus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            patch_tool = runtime.tools.get(PATCH_TOOL_ID)
            self.assertTrue(runtime.requires_consensus(patch_tool))
            git_tool = runtime.tools.get("TOOL-RUN-GIT")
            self.assertTrue(runtime.requires_consensus(git_tool))
            self.assertEqual(
                getattr(patch_tool, "side_effect_classification", ""),
                "mutation")

    def test_patch_apply_without_consensus_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "value.txt"
            source.write_text("old\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            decision = runtime.propose_patch_decision("")
            result = runtime.apply_patch(
                PatchApplier(root), PATCH, decision=decision)
            self.assertEqual(result.status, "REJECTED")
            self.assertEqual(
                result.metadata.get("error_code"), "CONSENSUS_REQUIRED")
            self.assertEqual(source.read_text(encoding="utf-8"), "old\n")

    def test_git_command_without_consensus_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _runtime(Path(directory) / "state")
            runtime.set_workspace_root(root)
            import subprocess
            subprocess.run(["git", "init", "-q"], cwd=root, check=False)
            result = runtime.invoke_tool(
                "TOOL-RUN-GIT", {"args": ["status"]}, objective="git status")
            self.assertEqual(result.status, "REJECTED")
            self.assertEqual(
                result.metadata.get("error_code"), "CONSENSUS_REQUIRED")

    def test_agreed_consensus_unblocks_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "value.txt"
            source.write_text("old\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            decision = runtime.propose_patch_decision("")
            runtime.record_consensus(
                decision.id, True, 0.9, models=["a/x", "b/y"],
                responses={"a/x": "same", "b/y": "same"})
            result = runtime.apply_patch(
                PatchApplier(root), PATCH, decision=decision)
            self.assertTrue(result.ok())
            self.assertEqual(source.read_text(encoding="utf-8"), "new\n")

    def test_disagreement_escalates_to_approval_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "value.txt"
            source.write_text("old\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            decision = runtime.propose_patch_decision("")
            runtime.record_consensus(
                decision.id, False, 0.2, models=["a/x", "b/y"],
                responses={"a/x": "one answer", "b/y": "other answer"})
            blocked = runtime.apply_patch(
                PatchApplier(root), PATCH, decision=decision)
            self.assertEqual(blocked.status, "REJECTED")
            self.assertEqual(
                blocked.metadata.get("error_code"), "APPROVAL_REQUIRED")
            self.assertEqual(source.read_text(encoding="utf-8"), "old\n")

            runtime.approve_decision(
                decision.id, runtime.authority, note="operator reviewed")
            allowed = runtime.apply_patch(
                PatchApplier(root), PATCH, decision=decision)
            self.assertTrue(allowed.ok())
            self.assertEqual(source.read_text(encoding="utf-8"), "new\n")

    def test_disagreed_mutation_blocked_even_when_risk_is_medium(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "value.txt"
            source.write_text("old\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            decision = runtime.propose_patch_decision("")
            runtime.record_consensus(decision.id, False, 0.1)
            result = runtime.apply_patch(
                PatchApplier(root), PATCH, decision=decision)
            self.assertEqual(result.status, "REJECTED")
            self.assertIn("APPROVAL_REQUIRED",
                          result.metadata.get("error_code", ""))

    def test_operator_approval_substitutes_for_consensus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "value.txt"
            source.write_text("old\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            decision = runtime.propose_patch_decision("")
            runtime.approve_decision(
                decision.id, "operator",
                note="consensus skipped by operator flag --no-consensus")
            result = runtime.apply_patch(
                PatchApplier(root), PATCH, decision=decision)
            self.assertTrue(result.ok())
            self.assertEqual(source.read_text(encoding="utf-8"), "new\n")
            self.assertNotIn(decision.id, runtime.consensus_records)


class ConsensusPersistenceTests(unittest.TestCase):
    def test_consensus_records_persist_across_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            runtime = _runtime(state)
            decision = runtime.propose_patch_decision("")
            runtime.record_consensus(
                decision.id, True, 0.85, models=["a/x", "b/y"],
                responses={"a/x": "same", "b/y": "same"})
            runtime.persist()

            restored = VialRuntime(_reference(), state)
            record = restored.consensus_records.get(decision.id)
            self.assertIsNotNone(record)
            self.assertTrue(record.agreed)
            self.assertAlmostEqual(record.agreement_ratio, 0.85)
            self.assertEqual(record.models, ["a/x", "b/y"])
            self.assertEqual(record.responses["a/x"], "same")

    def test_pending_decisions_report_consensus_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            decision = runtime.propose_patch_decision("")
            runtime.record_consensus(decision.id, False, 0.3)
            pending = runtime.pending_decisions()
            self.assertTrue(any(
                row["decision_id"] == decision.id
                and row["requires_consensus"]
                and row["consensus"]["agreed"] is False
                for row in pending))

    def test_record_consensus_roundtrip_via_to_dict_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            decision = runtime.propose_patch_decision("")
            record = runtime.record_consensus(decision.id, True, 0.99)
            self.assertEqual(record.decision_id, decision.id)
            self.assertEqual(len(record.models), 0)
            self.assertEqual(len(record.responses), 0)
            self.assertGreater(record.timestamp, 0)

    def test_consensus_note_persists_across_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            runtime = _runtime(state)
            decision = runtime.propose_patch_decision("")
            runtime.record_consensus(
                decision.id, True, 0.0,
                note="documented review outcome")
            runtime.persist()

            restored = VialRuntime(_reference(), state)
            record = restored.consensus_records.get(decision.id)
            self.assertIsNotNone(record)
            self.assertIn(
                "documented review outcome",
                record.note)

    def test_consensus_rejection_persists_decision_and_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            runtime = _runtime(state)
            decision = runtime.propose_patch_decision("")
            runtime.record_consensus(decision.id, False, 0.1)
            root = Path(directory) / "work"
            root.mkdir()
            source = root / "value.txt"
            source.write_text("old\n", encoding="utf-8")
            result = runtime.apply_patch(
                PatchApplier(root), PATCH, decision=decision)
            self.assertEqual(result.status, "REJECTED")
            self.assertIn("APPROVAL_REQUIRED",
                          result.metadata.get("error_code", ""))

            restored = VialRuntime(_reference(), state)
            self.assertIn(decision.id, restored.decision_engine.decisions)
            self.assertIn(decision.id, restored.consensus_records)


if __name__ == "__main__":
    unittest.main()