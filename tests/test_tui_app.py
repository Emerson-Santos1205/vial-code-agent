from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from vial_code_agent.app import ModelPicker, SelectableLog, SessionPicker, VialTUI
from vial_code_agent.chat import ChatController
from vial_code_agent.model import ModelResponse, OpenCodeProvider
from vial_code_agent.session import SessionStore


class _FakeProvider:
    """In-memory provider so TUI tests never spawn the ``opencode`` CLI."""

    MODEL_ALIASES = OpenCodeProvider.MODEL_ALIASES

    def __init__(self, model: str = "openai/gpt-5.6-luna-fast") -> None:
        self.model = self.MODEL_ALIASES.get(model, model)
        self.executable = "opencode"
        self.auto_approve = False
        self.agent = "plan"
        self._active_proc = None
        self.last_response: ModelResponse | None = None

    def chat(self, prompt, directory=None, timeout_seconds=180, history=None) -> ModelResponse:
        return ModelResponse(f"fake response: {prompt}", 0)

    def chat_stream(self, prompt, directory=None, timeout_seconds=180, history=None):
        for chunk in ("fake ", "response"):
            yield chunk
        self.last_response = ModelResponse("fake response", 0)

    def list_models(self, provider=None) -> str:
        return "opencode/deepseek-v4-flash-free\nmy-llm/gpt-4o\n"

    def list_providers(self) -> str:
        return "provider: openai\nprovider: my-llm\n"

    def cancel(self) -> None:
        pass


def _controller(directory: str) -> ChatController:
    root = Path(directory)
    store = SessionStore(root / "sessions")
    session = store.create()
    provider = _FakeProvider("openai/gpt-5.6-luna-fast")
    controller = ChatController(
        root, store, session, provider,
        "openai/gpt-5.6-luna-fast", "opencode", False, "plan",
    )
    controller.routing._provider_for = lambda ref: provider
    return controller


class TuiAppTests(unittest.TestCase):
    def test_tab_switches_agent(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    before = controller.agent
                    await pilot.press("tab")
                    await pilot.pause()
                    assert controller.agent != before

        asyncio.run(run())

    def test_ctrl_p_opens_model_picker(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    await pilot.press("ctrl+p")
                    await pilot.pause()
                    assert isinstance(app.screen, ModelPicker)
                    await pilot.press("escape")
                    await pilot.pause()

        asyncio.run(run())

    def test_picker_selection_pins_model(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    await pilot.press("ctrl+p")
                    await pilot.pause()
                    list_view = app.screen.query_one("#model-list")
                    list_view.index = 1
                    await pilot.pause()
                    await pilot.press("enter")
                    await pilot.pause()
                    expected = controller.available_models()[1]
                    assert controller.model == expected
                    assert controller.routing.default_model == expected

        asyncio.run(run())

    def test_ctrl_b_opens_pool_picker_and_saves(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)
                registry = controller.registry
                registry.pool_add("openai/gpt-5.6-luna-fast")
                registry.pool_add("my-llm/gpt-4o")
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    await pilot.press("ctrl+b")
                    await pilot.pause()
                    from vial_code_agent.app import PoolPicker
                    assert isinstance(app.screen, PoolPicker)
                    list_view = app.screen.query_one("#pool-list")
                    list_view.index = 1
                    await pilot.pause()
                    await pilot.press("space")
                    await pilot.pause()
                    await pilot.press("enter")
                    await pilot.pause()
                    assert controller.registry.pool == ["openai/gpt-5.6-luna-fast"]

        asyncio.run(run())

    def test_slash_commands_update_state(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    prompt = app.query_one("#prompt")
                    prompt.text = "/model openai/gpt-5.6-luna"
                    await pilot.press("enter")
                    await pilot.pause()
                    assert controller.model == "openai/gpt-5.6-luna"

                    side = str(app.query_one("#side").render())
                    assert "pinned" in side
                    assert "Pool (inactive)" in side

                    prompt.text = "/model auto"
                    await pilot.press("enter")
                    await pilot.pause()
                    side = str(app.query_one("#side").render())
                    assert "auto" in side
                    assert "Pool (inactive)" not in side

                    prompt.text = "/status"
                    await pilot.press("enter")
                    await pilot.pause()
                    joined = "\n".join(str(line) for line in app.query_one("#log").lines)
                    assert "session:" in joined
                    assert "routing:" in joined

                    prompt.text = "/models"
                    await pilot.press("enter")
                    await pilot.pause()
                    joined = "\n".join(str(line) for line in app.query_one("#log").lines)
                    assert "opencode" in joined or "registered" in joined

        asyncio.run(run())

    def test_exit_command_stops_app(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    prompt = app.query_one("#prompt")
                    prompt.text = "/exit"
                    await pilot.press("enter")
                    await pilot.pause()
                    assert not pilot.app.is_running

        asyncio.run(run())

    def test_clear_starts_clean_visual_session(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    log = app.query_one("#log")
                    log.write("old session output")
                    prompt = app.query_one("#prompt")
                    prompt.text = "/clear"
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertNotIn("old session output", "\n".join(str(line) for line in log.lines))
                    self.assertEqual(controller.history, [])

        asyncio.run(run())

    def test_log_selection_is_box_scoped(self) -> None:
        async def run() -> None:
            from textual.events import MouseMove
            from textual.geometry import Offset
            from textual.selection import Selection
            from textual.widgets import RichLog

            from vial_code_agent.app import SelectableLog

            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    log = app.query_one("#log", SelectableLog)
                    self.assertTrue(log.allow_select)
                    self.assertFalse(app.query_one("#side").allow_select)
                    self.assertFalse(app.query_one("#stream").allow_select)
                    self.assertFalse(app.query_one("#prompt").allow_select)
                    self.assertFalse(app.query_one("#command-menu").allow_select)
                    log.write("alpha beta gamma")
                    await pilot.pause()
                    direct = log.get_selection(Selection(Offset(0, 0), Offset(9, 0)))
                    self.assertEqual(direct, ("alpha bet", "\n"))
                    await pilot.mouse_down(widget=log, offset=(0, 0))
                    await pilot.pause()
                    app.post_message(MouseMove(widget=log, x=5, y=0, delta_x=5, delta_y=0, button=0, shift=False, meta=False, ctrl=False))
                    await pilot.pause()
                    await pilot.mouse_up(widget=log, offset=(5, 0))
                    await pilot.pause()
                    self.assertIsInstance(app.query_one("#log", RichLog), SelectableLog)
                    selected = app.screen.get_selected_text()
                    self.assertIsNotNone(selected)
                    self.assertIn("alpha", selected)

        asyncio.run(run())

    def test_log_selection_ignores_external_endpoint(self) -> None:
        async def run() -> None:
            from textual.events import MouseMove

            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    log = app.query_one("#log", SelectableLog)
                    side = app.query_one("#side")
                    log.write("log line one\nlog line two")
                    side.update("side panel text")
                    await pilot.pause()
                    await pilot.mouse_down(widget=log, offset=(0, 0))
                    app.post_message(MouseMove(
                        widget=side, x=0, y=0, delta_x=1, delta_y=1,
                        button=0, shift=False, meta=False, ctrl=False,
                    ))
                    await pilot.pause()
                    await pilot.mouse_up(widget=side, offset=(0, 0))
                    await pilot.pause()
                    self.assertTrue(all(
                        isinstance(widget, SelectableLog)
                        for widget in app.screen.selections
                    ))
                    selected = app.screen.get_selected_text()
                    self.assertNotIn("side panel text", selected or "")

        asyncio.run(run())


    def test_command_menu_navigation(self) -> None:
        async def run() -> None:
            from textual.widgets import ListView

            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    menu = app.query_one("#command-menu", ListView)
                    self.assertFalse(menu.display)
                    await pilot.press("/")
                    await pilot.pause()
                    self.assertTrue(menu.display)
                    self.assertGreater(len(menu.children), 0)
                    await pilot.press("down")
                    await pilot.pause()
                    self.assertEqual(menu.index, 1)
                    app.query_one("#prompt").text = ""
                    await pilot.pause()
                    self.assertFalse(menu.display)
                    for ch in "/model":
                        await pilot.press(ch)
                        await pilot.pause()
                    self.assertTrue(menu.display)
                    self.assertLess(len(menu.children), 6)
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertFalse(menu.display)
                    self.assertEqual(app.query_one("#prompt").text, "")

        asyncio.run(run())

    def test_ctrl_j_inserts_newline_in_prompt(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    prompt = app.query_one("#prompt")
                    prompt.text = "first"
                    await pilot.press("ctrl+j")
                    await pilot.pause()
                    self.assertIn("\n", prompt.text)

        asyncio.run(run())

    def test_ctrl_s_opens_session_picker(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)
                controller.respond("list account balances")
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    await pilot.press("ctrl+s")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, SessionPicker)
                    await pilot.press("escape")
                    await pilot.pause()

        asyncio.run(run())

    def test_session_picker_filter_and_select(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)
                controller.respond("list account balances")
                first_id = controller.session_id
                controller.session_id = controller.store.create()
                controller.respond("refactor the transfer method")
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    await pilot.press("ctrl+s")
                    await pilot.pause()
                    picker = app.screen
                    self.assertIsInstance(picker, SessionPicker)
                    filter_input = picker.query_one("#session-filter")
                    filter_input.value = "balances"
                    await pilot.pause()
                    list_view = picker.query_one("#session-list")
                    self.assertEqual(len(list_view.children), 1)
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(controller.session_id, first_id)

        asyncio.run(run())

    def test_ctrl_k_when_idle_deletes_to_end_of_line(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    prompt = app.query_one("#prompt")
                    prompt.text = "delete me"
                    prompt.cursor = (0, 6)
                    await pilot.press("ctrl+k")
                    await pilot.pause()
                    self.assertNotIn("delete me", prompt.text)

        asyncio.run(run())

    def test_streaming_updates_stream_widget_and_hides_when_done(self) -> None:
        async def run() -> None:
            from textual.widgets import LoadingIndicator, RichLog, Static

            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)

                def fake_stream(message: str, root=None, requested_model="auto", history=None):
                    for chunk in ("hello ", "world"):
                        import time
                        time.sleep(0.2)
                        yield chunk

                controller.routing.dispatch_stream = fake_stream
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    stream = app.query_one("#stream", Static)
                    self.assertFalse(stream.display)
                    prompt = app.query_one("#prompt")
                    prompt.text = "stream this"
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertTrue(app._busy)
                    self.assertTrue(app.query_one("#spinner", LoadingIndicator).display)
                    for _ in range(50):
                        if not app._busy:
                            break
                        await pilot.pause(0.05)
                    await pilot.pause()
                    self.assertFalse(stream.display)
                    self.assertFalse(app.query_one("#spinner", LoadingIndicator).display)
                    joined = "\n".join(
                        str(line) for line in app.query_one("#log", RichLog).lines
                    )
                    self.assertIn("hello world", joined)
                    messages = controller.store.messages(controller.session_id)
                    self.assertEqual(messages[1].content, "hello world")

        asyncio.run(run())

    def test_ctrl_k_cancels_active_stream(self) -> None:
        async def run() -> None:
            from textual.widgets import RichLog, Static

            import threading

            with tempfile.TemporaryDirectory() as directory:
                controller = _controller(directory)
                release = threading.Event()

                def slow_stream(message: str):
                    while not release.is_set():
                        yield "."
                        release.wait(0.02)
                    yield "finished"

                controller.respond_stream = slow_stream
                app = VialTUI(controller)
                async with app.run_test() as pilot:
                    prompt = app.query_one("#prompt")
                    prompt.text = "cancel me"
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertTrue(app._busy)
                    await pilot.press("ctrl+k")
                    await pilot.pause()
                    self.assertFalse(app._busy)
                    joined = "\n".join(
                        str(line) for line in app.query_one("#log", RichLog).lines
                    )
                    self.assertIn("task cancelled", joined)
                    self.assertFalse(app.query_one("#stream", Static).display)
                    release.set()

        asyncio.run(run())



if __name__ == "__main__":
    unittest.main()
