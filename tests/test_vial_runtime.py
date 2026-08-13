from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.core import VialCoreReference
from vial_code_agent.vial_runtime import VialRuntime


class VialRuntimeTests(unittest.TestCase):
    def test_composes_official_contracts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        reference = VialCoreReference(root / "vendor" / "vial-core")
        if not reference.exists():
            self.skipTest("VIAL submodule is not initialized")
        with tempfile.TemporaryDirectory() as directory:
            runtime = VialRuntime(reference, Path(directory))
            resource = runtime.register_resource("RESOURCE-LLM", "model", "cognition")
            self.assertTrue(resource.has_capability("cognition"))
            self.assertEqual(runtime.snapshot()["organization_id"], "ORG-VIAL-CODE-AGENT")
