from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from vial_code_agent.agent import CodeAgent
from vial_code_agent.core import VialCoreReference
from vial_code_agent.model import ModelResponse
from vial_code_agent.patches import PatchApplier
from vial_code_agent.router import (
    VialRouter, deterministic_solvable, resolve_deterministic,
)
from vial_code_agent.vial_runtime import (
    PATCH_TOOL_ID, RESOURCE_ORDER, VialRuntime,
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


class FullIntegrationTests(unittest.TestCase):
    # ------------------------------------------------------------------ #
    # state + coordinator: organizational State (RFC-003) and transitions
    # ------------------------------------------------------------------ #
    def test_organization_state_versions_and_authorized_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            org = runtime.organization
            self.assertEqual(org.org_id, "ORG-VIAL-CODE-AGENT")
            self.assertEqual(org.authority, "org-root")
            before = org.state_version
            org.add_field("answer", 42, ["facts"])
            self.assertGreater(org.config_version, 0)
            transition = org.transition(
                "answer", 43, actor=org.authority, operation="update",
                provenance="test")
            self.assertEqual(transition.resulting_version, before + 1)
            self.assertEqual(org.get("answer").value, 43)

    def test_state_rejects_unauthorized_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            org = runtime.organization
            org.add_field("answer", 42, ["facts"])
            with self.assertRaises(PermissionError):
                org.transition("answer", 0, actor="intruder", operation="x",
                               provenance="test")

    # ------------------------------------------------------------------ #
    # context + tokenizer: selective projection (RFC-007, SDK-004)
    # ------------------------------------------------------------------ #
    def test_selective_context_lifecycle_and_token_counting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.py"
            source.write_text("return 42\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            context = runtime.build_context("explain a.py", root, [source])
            self.assertEqual(context.status, "FROZEN")
            self.assertEqual(context.mode, "selective")
            self.assertIn("state:file:a.py", context.references)
            self.assertGreater(context.tokens, 0)
            self.assertEqual(
                context.tokens, runtime.count_tokens(context.body))
            context.consume()
            self.assertEqual(context.status, "CONSUMED")

    # ------------------------------------------------------------------ #
    # reuse: cognitive reuse with stale invalidation (RFC-008)
    # ------------------------------------------------------------------ #
    def test_cognitive_reuse_hit_and_stale_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.py"
            source.write_text("old\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            task = runtime.build_task(
                "trim trailing whitespace", [source], root, op="code_transform")
            context = runtime.build_context(
                "trim trailing whitespace", root, [source])
            runtime.store_reuse(task, "PATCH-A", 1.0, context)
            entry, outcome = runtime.lookup_reuse(task)
            self.assertEqual(outcome, "hit")
            self.assertEqual(entry.outcome, "PATCH-A")
            self.assertEqual(runtime.reuse_stats()["reuse_hits"], 1)

            source.write_text("changed\n", encoding="utf-8")
            runtime.add_workspace_fields(root, [source])
            entry, outcome = runtime.lookup_reuse(task)
            self.assertEqual(outcome, "stale")
            self.assertIsNone(entry)
            self.assertEqual(runtime.reuse_stats()["invalidations"], 1)

    # ------------------------------------------------------------------ #
    # reuse + determinism: reason once, reuse many times (RFC-008, RFC-010)
    # ------------------------------------------------------------------ #
    def test_agent_reuse_returns_cached_patch_without_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.py"
            source.write_text("x = 1   \n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            provider = Mock()
            agent = CodeAgent(provider, runtime=runtime)
            first = agent.generate("trim trailing whitespace", root, [source])
            self.assertEqual(first.route, "deterministic")
            self.assertIsNotNone(first.patch)
            provider.generate.assert_not_called()

            second = agent.generate("trim trailing whitespace", root, [source])
            self.assertTrue(second.reused)
            self.assertEqual(second.route, "reuse")
            self.assertEqual(second.patch, first.patch)
            provider.generate.assert_not_called()

    def test_deterministic_noop_does_not_call_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.py"
            source.write_text("x = 1\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            provider = Mock()
            agent = CodeAgent(provider, runtime=runtime)
            result = agent.generate("trim trailing whitespace", root, [source])
            self.assertEqual(result.route, "deterministic")
            self.assertIsNone(result.patch)
            provider.generate.assert_not_called()

    # ------------------------------------------------------------------ #
    # cost + selector: economic model + Deterministic First (RFC-010)
    # ------------------------------------------------------------------ #
    def test_cost_model_accounting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            runtime.record_inference(1_000, 500, tier="advanced")
            runtime.record_retrieval(2)
            runtime.record_construction(1)
            runtime.record_validation(1)
            costs = runtime.costs()
            self.assertGreater(costs["tokens"], 0)
            self.assertGreater(costs["inference"], 0)
            self.assertGreater(costs["total"], 0)

    def test_deterministic_first_selector_and_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            self.assertEqual(
                runtime.selector.select(True, []), "deterministic")
            self.assertEqual(
                runtime.selector.select(False, ["light", "advanced"]), "light")
            self.assertEqual(
                runtime.select_route("x", "auto", deterministic=True), None)
            self.assertEqual(
                runtime.select_route("x", "auto", deterministic=False), "reasoning")
            self.assertEqual(
                runtime.select_route("x", "openai/gpt-5.6-luna"), "openai/gpt-5.6-luna")

    def test_vial_router_uses_mechanical_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            router = VialRouter(runtime)
            self.assertEqual(
                router.route("trim trailing whitespace"), None)
            self.assertEqual(router.route("implement persistence"), "reasoning")
            self.assertEqual(router.route("explain this module"), "reasoning")

    def test_mechanical_transform_produces_applyable_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.py"
            source.write_text("x = 1   \n", encoding="utf-8")
            self.assertTrue(deterministic_solvable("trim trailing whitespace"))
            patch = resolve_deterministic(
                "trim trailing whitespace", root, [source])
            self.assertIsNotNone(patch)
            PatchApplier(root).apply(patch)
            self.assertEqual(source.read_text(encoding="utf-8"), "x = 1\n")

    # ------------------------------------------------------------------ #
    # executor: DeterministicExecutor + Evaluator (RFC-007 §2.2)
    # ------------------------------------------------------------------ #
    def test_deterministic_executor_numeric_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            runtime.organization.add_field("temp", 42, ["monitor"])
            context = runtime._context.ContextBuilder(
                runtime.organization).build_full(runtime._context.Task(
                    id="TASK-RANGE", prompt="range check", required=["temp"],
                    expected=True, op="range", args=["temp", 0, 100]))
            result = runtime.run_deterministic_executor(
                runtime._context.Task(
                    id="TASK-RANGE", prompt="range check", required=["temp"],
                    expected=True, op="range", args=["temp", 0, 100]),
                context)
            self.assertTrue(result.correct)
            self.assertEqual(result.quality, 1.0)

    # ------------------------------------------------------------------ #
    # decision + authorization + tool (SDK-005, TOOLS-001, TOOLS-007)
    # ------------------------------------------------------------------ #
    def test_decision_lifecycle_and_unauthorized_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            authority = runtime._decision.Authority(
                actor=runtime.authority, role="org-root",
                scope="organization", policy="code-apply")
            decision = runtime.decision_engine.propose(
                "objective", runtime.actor, authority, type="patch_apply")
            self.assertEqual(decision.status, "DRAFT")
            runtime.decision_engine.approve(decision.id, runtime.actor)
            self.assertEqual(decision.status, "PENDING")
            runtime.decision_engine.authorize(decision.id, runtime.authority)
            self.assertEqual(decision.status, "AUTHORIZED")
            with self.assertRaises(PermissionError):
                runtime.decision_engine.authorize(decision.id, "intruder")

    def test_authorization_gate_rejects_unauthenticated_flows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            tool = runtime.tools.get(PATCH_TOOL_ID)

            def invoke(decision, actor=None, org=None, cid=""):
                return tool.invoke(
                    {"patch": "x", "_applier": lambda value: None},
                    actor=actor or runtime.authority,
                    organization_id=org or runtime.org_id,
                    context_id=cid, decision=decision)

            draft = runtime.decision_engine.propose(
                "objective", runtime.actor,
                runtime._decision.Authority(actor=runtime.authority,
                                            scope="organization",
                                            policy="code-apply"),
                type="patch_apply")
            result = invoke(draft)
            self.assertEqual(result.status, "REJECTED")
            self.assertIn("DECISION_NOT_AUTHORIZED",
                          result.metadata.get("error_code", ""))

            wrong = runtime.decision_engine.propose(
                "objective", runtime.actor,
                runtime._decision.Authority(actor=runtime.authority,
                                            scope="organization",
                                            policy="code-apply"),
                type="other")
            runtime.decision_engine.approve(wrong.id, runtime.actor)
            runtime.decision_engine.authorize(wrong.id, runtime.authority)
            result = invoke(wrong)
            self.assertEqual(result.status, "REJECTED")
            self.assertIn("CAPABILITY_NOT_AUTHORIZED",
                          result.metadata.get("error_code", ""))

            authorized = runtime.propose_patch_decision("")
            result = invoke(authorized, actor="intruder")
            self.assertEqual(result.status, "REJECTED")
            self.assertIn("ACTOR_NOT_AUTHORIZED",
                          result.metadata.get("error_code", ""))

            result = invoke(runtime.propose_patch_decision(""), org="ORG-OTHER")
            self.assertEqual(result.status, "REJECTED")
            self.assertIn("ORGANIZATION_MISMATCH",
                          result.metadata.get("error_code", ""))

    def test_authorized_patch_tool_applies_and_audits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "value.txt"
            source.write_text("old\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            decision = runtime.propose_patch_decision("CTX-AUDIT")
            runtime.record_consensus(decision.id, True, 1.0)
            result = runtime.apply_patch(
                PatchApplier(root), PATCH, context_id="CTX-AUDIT",
                decision=decision)
            self.assertTrue(result.ok())
            self.assertEqual(source.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(len(runtime.audit_records()), 1)
            self.assertEqual(runtime.audit_records()[0]["decision_id"],
                             decision.id)

    # ------------------------------------------------------------------ #
    # coordinator: atomicity, idempotency, recovery (RFC-009)
    # ------------------------------------------------------------------ #
    def test_apply_patch_is_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "value.txt"
            source.write_text("old\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            decision = runtime.propose_patch_decision("")
            runtime.record_consensus(decision.id, True, 1.0)
            first = runtime.apply_patch(
                PatchApplier(root), PATCH, decision=decision)
            self.assertTrue(first.ok())
            self.assertEqual(source.read_text(encoding="utf-8"), "new\n")
            replay = runtime.apply_patch(PatchApplier(root), PATCH)
            self.assertTrue(replay.ok())
            self.assertTrue(replay.metadata.get("recovered"))
            self.assertEqual(source.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(runtime.coordinator.duplicate_commits, 0)

    def test_interrupted_operation_resolves_from_intent_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "value.txt"
            source.write_text("old\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            op_id = "OP-INTERRUPTED"
            runtime.coordinator.begin(
                op_id, "workspace", op_id, runtime.authority)
            resolved = runtime.resolve_operation(op_id)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.status, "pending")
            decision = runtime.propose_patch_decision("")
            runtime.record_consensus(decision.id, True, 1.0)
            result = runtime.apply_patch(PatchApplier(root), PATCH,
                                         operation_id=op_id,
                                         decision=decision)
            self.assertTrue(result.ok())
            self.assertEqual(
                runtime.resolve_operation(op_id).status, "committed")

    def test_rollback_is_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "value.txt"
            source.write_text("old\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            decision = runtime.propose_patch_decision("")
            runtime.record_consensus(decision.id, True, 1.0)
            runtime.apply_patch(PatchApplier(root), PATCH, decision=decision)
            runtime.record_rollback(PATCH)
            op_id = hashlib.sha256(PATCH.encode("utf-8")).hexdigest()
            self.assertIn(
                "ROLLBACK-" + op_id,
                [intent.operation_id
                 for intent in runtime.coordinator.intents.values()])

    # ------------------------------------------------------------------ #
    # resource: capabilities and tiers (SDK-003)
    # ------------------------------------------------------------------ #
    def test_resource_registry_and_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            resource = runtime.register_resource(
                "RESOURCE-TEST", "model", "vision")
            self.assertTrue(resource.has_capability("vision"))
            self.assertFalse(resource.has_capability("hearing"))
            tiers = runtime.capable_tiers()
            self.assertEqual(tiers, ["deterministic", "advanced"])
            selected = runtime.registry.select("code_transform")
            self.assertIsNotNone(selected)
            self.assertEqual(selected.resource_id, "RESOURCE-DETERMINISTIC")

    # ------------------------------------------------------------------ #
    # identity: Authenticator + Principal (SDK-001 §30)
    # ------------------------------------------------------------------ #
    def test_identity_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            principal = runtime.authenticate(runtime.actor, DEV_SECRET)
            self.assertEqual(principal.actor, runtime.actor)
            self.assertEqual(principal.organization_id, runtime.org_id)
            with self.assertRaises(PermissionError):
                runtime.authenticate(runtime.actor, "wrong-secret")
            with self.assertRaises(PermissionError):
                runtime.authenticate("unknown-actor", DEV_SECRET)

    # ------------------------------------------------------------------ #
    # persistence: organizational continuity (RFC-003)
    # ------------------------------------------------------------------ #
    def test_persistence_restores_organizational_cognition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            root = Path(directory)
            source = root / "a.py"
            source.write_text("old\n", encoding="utf-8")
            target = root / "value.txt"
            target.write_text("old\n", encoding="utf-8")
            runtime = _runtime(state)
            task = runtime.build_task(
                "trim trailing whitespace", [source], root, op="code_transform")
            context = runtime.build_context(
                "trim trailing whitespace", root, [source])
            runtime.store_reuse(task, "PATCH-PERSISTED", 1.0, context)
            decision = runtime.propose_patch_decision("")
            runtime.record_consensus(decision.id, True, 1.0)
            runtime.apply_patch(PatchApplier(root), PATCH, decision=decision)
            runtime.persist()

            restored = VialRuntime(_reference(), state)
            restored_task = restored.build_task(
                "trim trailing whitespace", [source], root, op="code_transform")
            signature = restored._reuse.reuse_signature(restored_task)
            self.assertEqual(
                restored.reuse_engine.cache[signature].outcome, "PATCH-PERSISTED")
            self.assertGreater(restored.organization.state_version, 0)
            self.assertEqual(len(restored.decision_engine.decisions), 1)
            self.assertEqual(len(restored.coordinator.intents), 1)
            self.assertEqual(len(restored.patch_tool.audit_records), 1)

    def test_reuse_stats_persist_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            root = Path(directory)
            source = root / "a.py"
            source.write_text("x = 1   \n", encoding="utf-8")
            runtime = _runtime(state)
            task = runtime.build_task(
                "trim trailing whitespace", [source], root, op="code_transform")
            context = runtime.build_context(
                "trim trailing whitespace", root, [source])
            runtime.store_reuse(task, "PATCH-A", 1.0, context)
            entry, outcome = runtime.lookup_reuse(task)
            self.assertEqual(outcome, "hit")
            runtime.persist()

            raw = json.loads((state / "reuse.json").read_text(encoding="utf-8"))
            self.assertIn("_stats", raw)
            self.assertIn("cache", raw)
            self.assertEqual(raw["_stats"]["reuse_hits"], 1)
            self.assertEqual(raw["_stats"]["recomputes"], 1)

            restored = VialRuntime(_reference(), state)
            self.assertEqual(restored.reuse_stats()["reuse_hits"], 1)
            self.assertEqual(restored.reuse_stats()["recomputes"], 1)
            self.assertEqual(restored.reuse_stats()["invalidations"], 0)
            restored_task = restored.build_task(
                "trim trailing whitespace", [source], root, op="code_transform")
            self.assertEqual(restored.lookup_reuse(restored_task)[1], "hit")

    def test_run_tool_enforces_command_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _runtime(Path(directory) / "state")
            runtime.set_workspace_root(root)
            rejected = runtime.invoke_tool(
                "TOOL-RUN-BUILD", {"command": "format-disk"},
                objective="run arbitrary command")
            self.assertEqual(rejected.status, "FAILED")
            self.assertIn("allowlist", (rejected.error or "").lower())

            allowed = runtime.invoke_tool(
                "TOOL-RUN-BUILD",
                {"command": [sys.executable, "-c", "print('ok')"]},
                objective="run allowlisted command")
            self.assertEqual(allowed.status, "SUCCESS")
            self.assertIn("ok", allowed.output["stdout"])

            unsafe = runtime.invoke_tool(
                "TOOL-RUN-BUILD",
                {"command": "format-disk", "unsafe": True},
                objective="run unrestricted command")
            self.assertEqual(unsafe.status, "FAILED")

    def test_persistence_restores_deterministic_executions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            runtime = _runtime(state)
            runtime.record_deterministic(
                runtime.build_task("trim trailing whitespace", [], state,
                                   op="code_transform"),
                runtime.build_context("trim trailing whitespace", state, []),
                "PATCH", correct=True, quality=1.0)
            runtime.record_validation(1)
            runtime.persist()

            restored = VialRuntime(_reference(), state)
            self.assertEqual(len(restored.executions), 1)
            self.assertEqual(restored.executions[0]["correct"], True)
            self.assertEqual(restored.costs()["validation"], runtime.costs()["validation"])

    def test_events_and_project_state_persist_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            root = Path(directory)
            source = root / "a.py"
            source.write_text("x = 1\n", encoding="utf-8")
            runtime = _runtime(state)
            runtime.publish_event(
                "RESOURCE_UPDATED", "RES-042", 17,
                data={"hint": "API criada"})
            runtime.capture_project(root, [source])
            runtime.set_project_status("backend", "complete")
            runtime.persist()

            restored = VialRuntime(_reference(), state)
            self.assertEqual(restored.events.stats()["events"], 1)
            self.assertEqual(
                restored.event_latest("RES-042").data["hint"], "API criada")
            self.assertEqual(
                restored.project.snapshot.status["backend"], "complete")
            self.assertIn("events", restored.snapshot())
            self.assertIn("project", restored.snapshot())
            with self.assertRaises(PermissionError):
                restored.publish_event("E", "RES", 1, actor="intruder")

    def test_contexts_persist_and_trace_reconstructs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            root = Path(directory)
            source = root / "a.py"
            source.write_text("x = 1\n", encoding="utf-8")
            runtime = _runtime(state)
            context = runtime.build_context(
                "trim trailing whitespace", root, [source])
            decision = runtime.propose_patch_decision(context.context_id)
            runtime.record_consensus(decision.id, True, 1.0)
            runtime.apply_patch(
                PatchApplier(root), "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 1\n",
                context.context_id, decision=decision)
            runtime.persist()

            restored = VialRuntime(_reference(), state)
            self.assertIn(context.context_id, restored.contexts)
            self.assertEqual(
                restored.contexts[context.context_id].objective,
                context.objective)
            trace = restored.decision_trace(decision.id)
            self.assertIsNotNone(trace["context"])
            self.assertEqual(trace["context"]["context_id"], context.context_id)

    def test_multi_agent_team_publishes_events(self) -> None:
        from vial_code_agent.agents import Agent, MultiAgentTeam
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            team = MultiAgentTeam(
                [Agent("reviewer", "review", lambda task: f"review:{task}"),
                 Agent("tester", "test", lambda task: f"test:{task}")],
                events=runtime.events, actor=runtime.actor)
            team.run("change")
            self.assertEqual(runtime.events.stats()["events"], 2)
            self.assertEqual(runtime.event_latest("tester").type, "AGENT_RUN")
            self.assertEqual(runtime.event_latest("tester").data["outcome"], "test:change")

    # ------------------------------------------------------------------ #
    # errors: structured VIAL error model (SDK-001 §30-31)
    # ------------------------------------------------------------------ #
    def test_structured_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _runtime(Path(directory))
            with self.assertRaises(runtime._errors.VIALValidationError) as cm:
                runtime.registry.register(runtime._resource.Resource(
                    "R", "model", "ORG-OTHER"))
            self.assertEqual(cm.exception.code, "ORGANIZATION_MISMATCH")

    # ------------------------------------------------------------------ #
    # agent: model path records costs and stores reuse (RFC-004, RFC-008)
    # ------------------------------------------------------------------ #
    def test_agent_model_path_stores_reuse_and_costs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.py"
            source.write_text("old\n", encoding="utf-8")
            runtime = _runtime(Path(directory) / "state")
            provider = Mock()
            provider.generate.return_value = ModelResponse(
                "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n", 0,
                input_tokens=100, output_tokens=50)
            agent = CodeAgent(provider, runtime=runtime)
            result = agent.generate("implement feature", root, [source])
            self.assertEqual(result.route, "reasoning")
            self.assertFalse(result.reused)
            self.assertGreater(runtime.costs()["total"], 0)
            entry, outcome = runtime.lookup_reuse(
                runtime.build_task("implement feature", [source], root))
            self.assertEqual(outcome, "hit")
