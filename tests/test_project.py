from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.project import ProjectStateStore


class ProjectStateTests(unittest.TestCase):
    def test_capture_and_delta(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.py"
            source.write_text("x = 1\n", encoding="utf-8")
            store = ProjectStateStore()
            store.configure({"vial-code-agent"})
            self.assertIsNone(store.delta_from(root, [source]))
            self.assertEqual(store.snapshot.files["a.py"].lines, 1)

            source.write_text("x = 2\n", encoding="utf-8")
            delta = store.delta_from(root, [source])
            self.assertIsNotNone(delta)
            self.assertEqual(delta.version, 1)
            self.assertEqual(len(delta.changed), 1)
            self.assertEqual(delta.changed[0][0], "a.py")

    def test_delta_reports_added_and_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "a.py"
            second = root / "b.py"
            first.write_text("x\n", encoding="utf-8")
            store = ProjectStateStore()
            store.delta_from(root, [first])
            second.write_text("y\n", encoding="utf-8")
            delta = store.delta_from(root, [first, second])
            self.assertEqual(delta.added, ["b.py"])
            first.unlink()
            delta = store.delta_from(root, [second])
            self.assertEqual(delta.removed, ["a.py"])

    def test_first_delta_establishes_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.py"
            source.write_text("x\n", encoding="utf-8")
            store = ProjectStateStore()
            self.assertIsNone(store.delta_from(root, [source]))

    def test_set_status_requires_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.py"
            source.write_text("x\n", encoding="utf-8")
            store = ProjectStateStore()
            store.configure({"vial-code-agent"})
            store.restore(store.capture(root, [source]))
            with self.assertRaises(PermissionError):
                store.set_status("backend", "complete", "intruder")
            store.set_status("backend", "complete", "vial-code-agent")
            self.assertEqual(store.snapshot.status["backend"], "complete")

    def test_snapshot_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.py"
            source.write_text("x\n", encoding="utf-8")
            store = ProjectStateStore()
            store.configure({"vial-code-agent"})
            snapshot = store.capture(root, [source])
            store.restore(snapshot)
            store.set_status("tests", "37/40", "vial-code-agent")
            restored = ProjectStateStore()
            restored.restore(type(snapshot).from_dict(snapshot.to_dict()))
            self.assertEqual(restored.snapshot.status["tests"], "37/40")


if __name__ == "__main__":
    unittest.main()
