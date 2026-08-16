from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vial_code_agent.chat import ChatController, _friendly_model_error, _models_output
from vial_code_agent.model import ModelResponse, OpenCodeProvider
from vial_code_agent.session import SessionStore


class _FakeDecision:
    model = "openai/gpt-5.6-luna-fast"
    tier = "advanced"


class _FakeDecisionDeterministic:
    model = "deterministic"
    tier = "deterministic"
    deterministic_keyword = "reuse"


class _FakeRouting:
    def __init__(self, decision=None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.decision = decision or _FakeDecision()

    def dispatch(self, task, root=None, requested_model="auto", history=None):
        self.calls.append((task, history))
        return ModelResponse("answer", 0), self.decision

    def model_for(self, task, requested_model="auto"):
        return requested_model

    def dispatch_stream(self, task, root=None, requested_model="auto", history=None):
        self.calls.append((task, history))
        for chunk in ("streamed", " ", "answer"):
            yield chunk

    def cancel_active(self) -> None:
        self.cancelled = True


class ChatMemoryTests(unittest.TestCase):
    def _controller(self, directory: str) -> tuple[ChatController, _FakeRouting]:
        root = Path(directory)
        store = SessionStore(root / "sessions")
        controller = ChatController(
            root, store, store.create(),
            OpenCodeProvider("openai/gpt-5.6-luna-fast"),
            "auto", "opencode", False, "plan",
        )
        routing = _FakeRouting()
        controller.routing = routing
        return controller, routing

    def test_first_prompt_has_no_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, routing = self._controller(directory)
            controller.respond("first question")
            self.assertEqual(routing.calls[0][1], [])

    def test_second_prompt_receives_prior_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, routing = self._controller(directory)
            controller.respond("create a function")
            controller.respond("translate the docstring")
            history = routing.calls[1][1]
            self.assertEqual(
                history,
                [("user", "create a function"), ("assistant", "answer")],
            )

    def test_resume_loads_session_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, routing = self._controller(directory)
            controller.respond("remember this phrase")
            resumed = ChatController(
                Path(directory), controller.store, controller.session_id,
                OpenCodeProvider("openai/gpt-5.6-luna-fast"),
                "auto", "opencode", False, "plan",
            )
            resumed.routing = routing
            resumed.respond("what did I ask?")
            self.assertEqual(
                routing.calls[-1][1][0], ("user", "remember this phrase"))

    def test_respond_stream_yields_chunks_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, routing = self._controller(directory)
            chunks = list(controller.respond_stream("explain the bank"))
            self.assertEqual("".join(chunks), "streamed answer")
            messages = controller.store.messages(controller.session_id)
            self.assertEqual(messages[0].role, "user")
            self.assertEqual(messages[0].content, "explain the bank")
            self.assertEqual(messages[1].role, "assistant")
            self.assertEqual(messages[1].content, "streamed answer")

    def test_cancel_stream_delegates_to_routing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, routing = self._controller(directory)
            self.assertFalse(getattr(routing, "cancelled", False))
            controller.cancel_stream()
            self.assertTrue(routing.cancelled)

    def test_session_previews_lists_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _routing = self._controller(directory)
            controller.respond("show account balances")
            other = ChatController(
                Path(directory), controller.store, controller.store.create(),
                OpenCodeProvider("openai/gpt-5.6-luna-fast"),
                "auto", "opencode", False, "plan",
            )
            other.respond("refactor the transfer method")
            previews = controller.session_previews()
            self.assertEqual(len(previews), 2)
            first_preview = next(
                (preview for sid, preview in previews if sid == other.session_id),
                None,
            )
            self.assertIsNotNone(first_preview)
            self.assertIn("refactor", first_preview)
            self.assertIn("2 msg", first_preview)


class ChatCommandTests(unittest.TestCase):
    def _controller(self, directory: str) -> ChatController:
        root = Path(directory)
        store = SessionStore(root / "sessions")
        controller = ChatController(
            root, store, store.create(),
            OpenCodeProvider("openai/gpt-5.6-luna-fast"),
            "auto", "opencode", False, "plan",
        )
        controller.routing = _FakeRouting()
        return controller

    def test_exit_sets_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._controller(directory).handle("/exit")
            self.assertTrue(result.exit)

    def test_help_returns_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._controller(directory).handle("/help")
            self.assertTrue(result.handled)
            self.assertIn("/models", result.output)

    def test_unknown_command_not_handled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._controller(directory).handle("/nope")
            self.assertFalse(result.handled)

    def test_clear_starts_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            result = controller.handle("/clear")
            self.assertTrue(result.handled)
            self.assertNotEqual(result.new_session_id, controller.session_id)

    def test_sessions_empty_and_listed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / "sessions")
            controller = ChatController(
                root, store, "missing",
                OpenCodeProvider("openai/gpt-5.6-luna-fast"),
                "auto", "opencode", False, "plan",
            )
            controller.routing = _FakeRouting()
            empty = controller.handle("/sessions")
            self.assertEqual(empty.output, "no sessions")
            controller.session_id = store.create()
            controller.respond("hello")
            listed = controller.handle("/sessions")
            self.assertIn("sessions (most recent first):", listed.output)

    def test_resume_usage_unknown_and_ok(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            self.assertIn("usage:", controller.handle("/resume").output)
            self.assertIn(
                "unknown session", controller.handle("/resume missing-id").output)
            self.assertIn(
                "resumed session", controller.handle(
                    f"/resume {controller.session_id}").output)

    def test_servers_empty_and_with_servers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            empty = controller.handle("/servers")
            self.assertIn("no servers configured", empty.output)
            controller.registry.add_server("local", "http://localhost:8000/v1", "KEY")
            listed = controller.handle("/servers")
            self.assertIn("local", listed.output)

    def test_server_add_remove_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            self.assertIn("usage:", controller.handle("/server add").output)
            added = controller.handle("/server add local http://localhost:8000/v1")
            self.assertIn("server added: local", added.output)
            dup = controller.handle("/server add local http://localhost:8000/v1")
            self.assertIn("error:", dup.output)
            self.assertIn("usage:", controller.handle("/server remove").output)
            self.assertIn(
                "server removed: local",
                controller.handle("/server remove local").output)
            self.assertIn("error:", controller.handle("/server remove nope").output)
            self.assertIn(
                "usage:", controller.handle("/server models").output)
            self.assertIn(
                "unknown server", controller.handle("/server models nope").output)
            self.assertIn(
                "unknown /server action",
                controller.handle("/server nope").output)

    def test_pool_add_remove_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            self.assertIn("pool:", controller.handle("/pool").output)
            self.assertIn("usage:", controller.handle("/pool add").output)
            self.assertIn("pool add:", controller.handle(
                "/pool add local/model").output)
            self.assertIn("usage:", controller.handle("/pool remove").output)
            self.assertIn("pool remove:", controller.handle(
                "/pool remove local/model").output)
            self.assertIn("usage:", controller.handle("/pool set").output)
            self.assertIn("pool set:", controller.handle(
                "/pool set local/a local/b").output)
            self.assertIn("unknown /pool action", controller.handle(
                "/pool nope").output)

    def test_model_add_remove_and_select(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            self.assertIn("usage:", controller.handle("/model").output)
            self.assertIn("usage:", controller.handle("/model add").output)
            self.assertIn(
                "error:", controller.handle("/model add not-server/model").output)
            controller.registry.add_server("local", "http://localhost:8000/v1")
            self.assertIn(
                "model added:", controller.handle("/model add local/m1").output)
            self.assertIn("usage:", controller.handle("/model remove").output)
            self.assertIn(
                "model removed:", controller.handle("/model remove local/m1").output)
            selected = controller.handle("/model openai/gpt-5.6-luna")
            self.assertIn("pinned", selected.output)
            self.assertEqual(selected.new_model, "openai/gpt-5.6-luna")
            auto = controller.handle("/model auto")
            self.assertIn("orchestrator", auto.output)

    def test_agent_switch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            invalid = controller.handle("/agent nope")
            self.assertIn("usage:", invalid.output)
            switched = controller.handle("/agent plan")
            self.assertEqual(switched.new_agent, "plan")

    def test_auto_toggles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            self.assertFalse(controller.auto_approve)
            first = controller.handle("/auto")
            self.assertIn("auto-approve: on", first.output)
            second = controller.handle("/auto")
            self.assertIn("auto-approve: off", second.output)

    def test_copy_usage_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            empty = controller.handle("/copy")
            self.assertEqual(empty.output, "no assistant response to copy")
            controller.respond("hello")
            copied = controller.handle("/copy")
            self.assertEqual(copied.clipboard, "answer")

    def test_events_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._controller(directory).handle("/events")
            self.assertIn("governed runtime unavailable", result.output)

    def test_delta_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._controller(directory).handle("/delta")
            self.assertIn("governed runtime unavailable", result.output)

    def test_trace_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            usage = controller.handle("/trace")
            self.assertIn("usage:", usage.output)
            result = controller.handle("/trace DEC-1")
            self.assertIn("governed runtime unavailable", result.output)

    def test_approve_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            usage = controller.handle("/approve")
            self.assertIn("usage:", usage.output)
            result = controller.handle("/approve DEC-1")
            self.assertIn("governed runtime unavailable", result.output)

    def test_providers_with_discovery_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)

            def boom():
                raise RuntimeError("no providers")

            controller.provider.list_providers = boom
            result = controller.handle("/providers")
            self.assertIn("error: no providers", result.output)

    def test_models_output_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            controller.registry.add_server("local", "http://localhost:8000/v1")
            controller.registry.add_model("local", "m1")
            controller.provider.list_models = lambda _: "opencode/x\n"
            output = _models_output(controller.registry, controller.provider, "")
            self.assertIn("registered:", output)
            self.assertIn("opencode/x", output)


class ChatErrorTests(unittest.TestCase):
    def _controller(self, directory: str) -> tuple[ChatController, _FakeRouting]:
        root = Path(directory)
        store = SessionStore(root / "sessions")
        controller = ChatController(
            root, store, store.create(),
            OpenCodeProvider("openai/gpt-5.6-luna-fast"),
            "auto", "opencode", False, "plan",
        )
        routing = _FakeRouting()
        controller.routing = routing
        return controller, routing

    def test_model_failure_uses_friendly_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / "sessions")
            controller = ChatController(
                root, store, store.create(),
                OpenCodeProvider("openai/gpt-5.6-luna-fast"),
                "auto", "opencode", False, "plan",
            )

            class _FailingRouting:
                def dispatch(self, *args, **kwargs):
                    return ModelResponse("", 1, stderr="cannot connect"), _FakeDecision()

            controller.routing = _FailingRouting()
            text, _ = controller.respond("hello")
            self.assertIn("Cannot connect", text)

    def test_deterministic_tier_prefixed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / "sessions")
            controller = ChatController(
                root, store, store.create(),
                OpenCodeProvider("openai/gpt-5.6-luna-fast"),
                "auto", "opencode", False, "plan",
            )
            routing = _FakeRouting(_FakeDecisionDeterministic())
            controller.routing = routing
            text, _ = controller.respond("hello")
            self.assertIn("[deterministic: reuse]", text)

    def test_prior_turns_ignores_corrupt_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / "sessions")
            controller = ChatController(
                root, store, store.create(),
                OpenCodeProvider("openai/gpt-5.6-luna-fast"),
                "auto", "opencode", False, "plan",
            )
            path = store.directory / f"{controller.session_id}.jsonl"
            path.write_text("{not json\n", encoding="utf-8")
            self.assertEqual(controller._prior_turns(), [])

    def test_last_assistant_returns_plain_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, routing = self._controller(directory)
            controller.respond("hello")
            self.assertEqual(controller.last_assistant(), "answer")

    def test_friendly_model_error_connect(self) -> None:
        text = _friendly_model_error("unable to connect to the provider", "openai/x")
        self.assertIn("Cannot connect", text)
        self.assertIn("openai/x", text)

    def test_available_models_handles_discovery_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _ = self._controller(directory)

            def boom():
                raise RuntimeError("boom")

            controller.provider.list_models = boom
            models = controller.available_models()
            self.assertEqual(models, ["auto"])


class _FakeEvent:
    def __init__(self, event_type: str, resource: str) -> None:
        self.type = event_type
        self.resource = resource
        self.version = 1
        self.actor = "tester"
        self.data = "{}"


class _FakeDecisionRecord:
    def __init__(self) -> None:
        self.decision_id = "DEC-1"
        self.approver = "tester"


class _FakeDelta:
    def to_dict(self) -> dict[str, object]:
        return {"state": "new"}


class _FakeRuntime:
    def __init__(self) -> None:
        self.events = [_FakeEvent("FILE_MODIFIED", "a.py")]
        self.decision = {"id": "DEC-1"}
        self.approval = _FakeDecisionRecord()
        self.approve_calls = 0

    def event_delta(self, after_event_id: str = "") -> list[_FakeEvent]:
        return self.events

    def project_delta(self, root, files):
        return _FakeDelta()

    def decision_trace(self, decision_id: str) -> dict[str, object]:
        if decision_id == "DEC-MISSING":
            raise KeyError(decision_id)
        return self.decision

    def approve_decision(self, decision_id, authority, note=""):
        self.approve_calls += 1
        if decision_id == "DEC-MISSING":
            raise KeyError(decision_id)
        return self.approval

    def pending_decisions(self) -> list[dict]:
        return [
            {
                "decision_id": "DEC-1",
                "objective": "apply patch",
                "risk": "medium",
                "requires_consensus": True,
                "consensus": None,
                "approval": None,
            },
        ]

    def persist(self) -> None:
        return None


class ChatRuntimeCommandTests(unittest.TestCase):
    def _controller(self, directory: str) -> ChatController:
        root = Path(directory)
        store = SessionStore(root / "sessions")
        controller = ChatController(
            root, store, store.create(),
            OpenCodeProvider("openai/gpt-5.6-luna-fast"),
            "auto", "opencode", False, "plan",
            runtime=_FakeRuntime(),
        )
        controller.routing = _FakeRouting()
        return controller

    def test_events_listed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._controller(directory).handle("/events")
            self.assertIn("FILE_MODIFIED", result.output)

    def test_delta_captured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self._controller(directory).handle("/delta")
            self.assertIn('"state"', result.output)

    def test_trace_ok_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            ok = controller.handle("/trace DEC-1")
            self.assertIn("DEC-1", ok.output)
            missing = controller.handle("/trace DEC-MISSING")
            self.assertIn("unknown decision", missing.output)

    def test_approve_ok_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            ok = controller.handle("/approve DEC-1")
            self.assertIn("approved by tester", ok.output)
            missing = controller.handle("/approve DEC-MISSING")
            self.assertIn("error:", missing.output)

    def test_decisions_lists_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            ok = controller.handle("/decisions")
            self.assertIn("DEC-1", ok.output)
            self.assertIn("consensus=missing", ok.output)

    def test_consensus_shows_missing_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            ok = controller.handle("/consensus")
            self.assertIn("DEC-1", ok.output)
            self.assertIn("missing", ok.output)

    def test_decisions_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / "sessions")
            controller = ChatController(
                root, store, store.create(),
                OpenCodeProvider("openai/gpt-5.6-luna-fast"),
                "auto", "opencode", False, "plan", runtime=None)
            missing = controller.handle("/decisions")
            self.assertIn("unavailable", missing.output)
            consensus = controller.handle("/consensus")
            self.assertIn("unavailable", consensus.output)


class ChatEdgeTests(unittest.TestCase):
    def _controller(self, directory: str) -> ChatController:
        return ChatCommandTests._controller(self, directory)

    def test_server_without_args_lists_servers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            result = controller.handle("/server")
            self.assertIn("no servers configured", result.output)

    def test_events_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / "sessions")
            runtime = _FakeRuntime()
            runtime.events = []
            controller = ChatController(
                root, store, store.create(),
                OpenCodeProvider("openai/gpt-5.6-luna-fast"),
                "auto", "opencode", False, "plan", runtime=runtime,
            )
            controller.routing = _FakeRouting()
            result = controller.handle("/events")
            self.assertEqual(result.output, "no events")

    def test_delta_baseline_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / "sessions")
            runtime = _FakeRuntime()
            runtime.project_delta = lambda root, files: None
            controller = ChatController(
                root, store, store.create(),
                OpenCodeProvider("openai/gpt-5.6-luna-fast"),
                "auto", "opencode", False, "plan", runtime=runtime,
            )
            controller.routing = _FakeRouting()
            result = controller.handle("/delta")
            self.assertIn("baseline captured", result.output)

    def test_copy_handles_corrupt_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / "sessions")
            controller = ChatController(
                root, store, store.create(),
                OpenCodeProvider("openai/gpt-5.6-luna-fast"),
                "auto", "opencode", False, "plan",
            )
            path = store.directory / f"{controller.session_id}.jsonl"
            path.write_text("{not json\n", encoding="utf-8")
            result = controller.handle("/copy")
            self.assertIn("no messages", result.output)

    def test_server_models_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory)
            controller.registry.add_server("local", "http://localhost:8000/v1")
            from vial_code_agent.chat import _server_models_output
            output = _server_models_output(controller.registry, "local")
            self.assertIn("(none; add with /model add", output)

    def test_last_assistant_handles_corrupt_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / "sessions")
            controller = ChatController(
                root, store, store.create(),
                OpenCodeProvider("openai/gpt-5.6-luna-fast"),
                "auto", "opencode", False, "plan",
            )
            path = store.directory / f"{controller.session_id}.jsonl"
            path.write_text("{not json\n", encoding="utf-8")
            self.assertEqual(controller.last_assistant(), "")

    def test_server_models_with_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = ChatCommandTests._controller(self, directory)
            controller.registry.add_server("local", "http://localhost:8000/v1")
            controller.registry.add_model("local", "m1")
            from vial_code_agent.chat import _server_models_output
            output = _server_models_output(controller.registry, "local")
            self.assertIn("local/m1", output)

    def test_models_output_no_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = ChatCommandTests._controller(self, directory)
            controller.provider.list_models = lambda _: ""
            from vial_code_agent.chat import _models_output
            output = _models_output(controller.registry, controller.provider, "")
            self.assertEqual(output, "no models available")

    def test_models_output_discovery_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = ChatCommandTests._controller(self, directory)
            controller.registry.add_server("local", "http://localhost:8000/v1")
            controller.registry.add_model("local", "m1")

            def boom(_):
                raise RuntimeError("discovery down")

            controller.provider.list_models = boom
            from vial_code_agent.chat import _models_output
            output = _models_output(controller.registry, controller.provider, "")
            self.assertIn("discovery error", output)

    def test_friendly_model_error_fallback(self) -> None:
        text = _friendly_model_error("random failure", "openai/x")
        self.assertIn("error: random failure", text)

    def test_model_add_invalid_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = ChatCommandTests._controller(self, directory)
            result = controller.handle("/model add no-slash")
            self.assertIn("error:", result.output)

    def test_model_remove_invalid_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = ChatCommandTests._controller(self, directory)
            result = controller.handle("/model remove no-slash")
            self.assertIn("error:", result.output)


if __name__ == "__main__":
    unittest.main()
