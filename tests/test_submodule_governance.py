from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vial_code_agent.core import VialCoreReference
from release_orchestrator.core import repo_health_issues, sync_core_submodule
from release_orchestrator.git import get_submodule_commit, check_submodule_drift


class SubmoduleGovernanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def test_vial_core_reference_commit_inspection(self) -> None:
        vial = VialCoreReference(self.root / "vendor" / "vial-core")
        if not vial.exists():
            self.skipTest("submodule vendor/vial-core not initialized")

        current = vial.get_current_commit()
        self.assertTrue(len(current) > 0, "Current commit SHA should not be empty")

        drift = vial.check_drift()
        self.assertTrue(drift["exists"])
        self.assertEqual(drift["current_commit"], current)

    def test_release_orchestrator_submodule_drift_check(self) -> None:
        drift = check_submodule_drift(self.root, "vendor/vial-core")
        self.assertTrue(drift["exists"])
        self.assertTrue(len(str(drift["current_sha"])) > 0)

    def test_sync_core_submodule_report(self) -> None:
        report = sync_core_submodule(self.root, update=False)
        self.assertTrue(report.exists)
        self.assertEqual(report.updated, False)

    def test_repo_health_checks_submodule_existence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            issues = repo_health_issues(tmp_path)
            self.assertIn("submodule vendor/vial-core not initialized", issues)


if __name__ == "__main__":
    unittest.main()
