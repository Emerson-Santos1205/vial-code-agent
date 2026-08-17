from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.core import VialCoreReference
from vial_code_agent.vial_runtime import PersistenceError, VialRuntime


class SnapshotPersistenceTests(unittest.TestCase):
    def test_corrupt_active_snapshot_is_rejected(self) -> None:
        root = Path(__file__).resolve().parents[1]
        reference = VialCoreReference(root / "vendor" / "vial-core")
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            runtime = VialRuntime(reference, state)
            runtime.persist()
            pointer = state / "current-snapshot.json"
            generation = __import__("json").loads(pointer.read_text())["generation"]
            organization = state / "snapshots" / generation / "organization.json"
            organization.write_text("corrupt", encoding="utf-8")
            with self.assertRaises(PersistenceError):
                VialRuntime(reference, state)
