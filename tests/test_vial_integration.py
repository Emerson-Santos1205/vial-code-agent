from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.core import VialCoreReference
from vial_code_agent.patches import PatchApplier


class VialIntegrationTests(unittest.TestCase):
    def test_official_context_lifecycle_and_authorized_patch_tool(self) -> None:
        root = Path(__file__).resolve().parents[1]
        vial = VialCoreReference(root / "vendor" / "vial-core")
        if not vial.exists():
            self.skipTest("VIAL submodule is not initialized")
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = workspace / "value.txt"
            source.write_text("old\n", encoding="utf-8")
            context = vial.build_context("update value", workspace, [source])
            self.assertEqual(context.status, "FROZEN")
            context.consume()
            patch = """--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
+new
"""
            result = vial.execute_patch(PatchApplier(workspace), patch, context.context_id)
            self.assertTrue(result.ok())
            self.assertEqual(source.read_text(encoding="utf-8"), "new\n")
