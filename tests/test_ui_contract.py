"""Test that chat.py and tui_state.py remain framework-free."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "vial_code_agent"


class FrameworkIsolationTests(unittest.TestCase):
    def test_chat_py_has_no_textual_imports(self) -> None:
        tree = ast.parse((SRC / "chat.py").read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
        textual = [i for i in imports if "textual" in i.lower() or "rich" in i.lower()]
        self.assertEqual(textual, [], f"chat.py imports UI frameworks: {textual}")

    def test_tui_state_py_has_no_textual_imports(self) -> None:
        tree = ast.parse((SRC / "tui_state.py").read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
        textual = [i for i in imports if "textual" in i.lower() or "rich" in i.lower()]
        self.assertEqual(textual, [], f"tui_state.py imports UI frameworks: {textual}")


if __name__ == "__main__":
    unittest.main()
