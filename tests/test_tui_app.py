from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from vial_code_agent.app import ModelPicker, VialTUI
from vial_code_agent.chat import ChatController
from vial_code_agent.model import OpenCodeProvider
from vial_code_agent.session import SessionStore


def _controller(directory: str) -> ChatController:
    root = Path(directory)
    store = SessionStore(root / "sessions")
    session = store.create()
    return ChatController(
        root, store, session,
        OpenCodeProvider("openai/gpt-5.6-luna-fast"),
        "openai/gpt-5.6-luna-fast", "opencode", False, "plan",
    )


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



if __name__ == "__main__":
    unittest.main()
