from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from vial_code_agent.agent import CodeAgent
from vial_code_agent.cognition import CognitionEngine, CognitionRequest
from vial_code_agent.core import VialCoreReference
from vial_code_agent.patches import PatchApplier
from vial_code_agent.vial_runtime import (
    PATCH_TOOL_ID, RISK_HIGH, VialRuntime,
)

DEV_SECRET = "local-vial-dev-secret"


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


class ToolCatalogTests(unittest.TestCase):
    def test_catalog_registers_governed_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            ids = {tool.tool_id for tool in runtime.tools.list()}
            for expected in (
                PATCH_TOOL_ID, "TOOL-READ-FILE", "TOOL-SEARCH",
                "TOOL-LIST-FILES", "TOOL-INSPECT-DEPENDENCY", "TOOL-RUN-TEST",
                "TOOL-RUN-BUILD", "TOOL-RUN-GIT", "TOOL-RUN-AUDIT",
            ):
                self.assertIn(expected, ids)

    def test_read_file_requires_authorized_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.py"
            source.write_text("x = 1\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            runtime.set_workspace_root(root)

            result = runtime.invoke_tool("TOOL-READ-FILE", {"path": "a.py"},
                                         objective="read a.py")
            self.assertEqual(result.status, "SUCCESS")
            self.assertEqual(result.output["content"], "x = 1\n")
            self.assertTrue(result.invocation_id.startswith("INV-"))
            self.assertEqual(len(runtime.audit_records()), 1)
            decision = next(iter(runtime.decision_engine.decisions.values()))
            self.assertEqual(decision.status, "COMPLETED")
            self.assertEqual(decision.outcome["status"], "SUCCESS")

    def test_search_and_list_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text("def hello():\n    pass\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            runtime.set_workspace_root(root)

            search = runtime.invoke_tool("TOOL-SEARCH", {"pattern": "hello"})
            self.assertEqual(search.status, "SUCCESS")
            self.assertEqual(search.output["matches"][0]["path"], "a.py")

            listed = runtime.invoke_tool(
                "TOOL-LIST-FILES", {"patterns": ["*.py"]})
            self.assertEqual(listed.status, "SUCCESS")
            self.assertEqual(listed.output["files"], ["a.py"])

    def test_high_risk_tool_requires_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _runtime(Path(directory) / "state")
            runtime.set_workspace_root(root)
            import subprocess
            subprocess.run(["git", "init", "-q"], cwd=root, check=False)

            no_consensus = runtime.invoke_tool(
                "TOOL-RUN-GIT", {"args": ["status"]}, objective="git status")
            self.assertEqual(no_consensus.status, "REJECTED")
            self.assertIn("CONSENSUS_REQUIRED",
                          no_consensus.metadata.get("error_code", ""))

            decision = runtime.propose_decision(
                "git status", "run_git", policy="development",
                risk=RISK_HIGH)
            runtime.record_consensus(decision.id, True, 1.0)
            rejected = runtime.invoke_tool(
                "TOOL-RUN-GIT", {"args": ["status"]}, objective="git status",
                decision=decision)
            self.assertEqual(rejected.status, "REJECTED")
            self.assertIn("APPROVAL_REQUIRED",
                          rejected.metadata.get("error_code", ""))

            runtime.approve_decision(decision.id, runtime.authority,
                                     note="operator approved")
            approved = runtime.invoke_tool(
                "TOOL-RUN-GIT", {"args": ["status"]}, objective="git status",
                decision=decision)
            self.assertEqual(approved.status, "SUCCESS")
            self.assertIsNotNone(approved.output["stdout"])

    def test_decision_trace_reconstructs_why(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "value.txt"
            source.write_text("old\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            context = runtime.build_context("update value", root, [source])
            decision = runtime.propose_patch_decision(context.context_id)
            runtime.record_consensus(decision.id, True, 1.0)
            runtime.apply_patch(PatchApplier(root), PATCH,
                                context_id=context.context_id,
                                decision=decision)
            trace = runtime.decision_trace(decision.id)
            self.assertTrue(trace["found"])
            self.assertEqual(trace["status"], "COMPLETED")
            self.assertEqual(trace["context"]["context_id"],
                             context.context_id)
            self.assertEqual(len(trace["audit_records"]), 1)
            self.assertIn("context:", trace["evidence"][0])

    def test_memory_exposed_in_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            self.assertIn("memory", runtime.snapshot())
            self.assertIn("decisions", runtime.snapshot()["memory"])
            self.assertIn("approvals", runtime.snapshot()["memory"])


class CognitionEngineTests(unittest.TestCase):
    def test_deterministic_proposal_without_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.py"
            source.write_text("x = 1\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            context = runtime.build_context(
                "trim trailing whitespace", root, [source])
            provider = Mock()
            engine = CognitionEngine(provider)
            request = CognitionRequest(
                cycle="CG-1", objective="trim trailing whitespace",
                context=context, authority="org-root",
                root=root, files=[source])
            result = engine.evaluate(request)
            self.assertTrue(result.deterministic)
            self.assertEqual(result.confidence, 1.0)
            self.assertEqual(result.decision_proposal, "no-op")
            provider.generate.assert_not_called()

    def test_model_proposal_from_mock_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.py"
            source.write_text("old\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            context = runtime.build_context("implement feature", root, [source])
            provider = Mock()
            provider.generate.return_value = Mock(
                text="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
                returncode=0, input_tokens=10, output_tokens=5)
            engine = CognitionEngine(provider)
            result = engine.evaluate(CognitionRequest(
                cycle="CG-2", objective="implement feature",
                context=context, authority="org-root",
                root=root, files=[source]))
            self.assertFalse(result.deterministic)
            self.assertIsNotNone(result.decision_proposal)
            self.assertIn("model_tokens_in:10", result.evidence)

    def test_agent_plan_cognition_uses_runtime_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.py"
            source.write_text("x = 1   \n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            agent = CodeAgent(Mock(), runtime=runtime)
            context = runtime.build_context(
                "trim trailing whitespace", root, [source])
            result = agent.plan_cognition(
                "trim trailing whitespace", root, [source], context=context)
            self.assertTrue(result.deterministic)
            self.assertTrue(result.cycle.startswith("CG-"))
