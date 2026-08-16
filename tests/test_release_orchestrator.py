from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from release_orchestrator.cli import main
from release_orchestrator.core import release_tag


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([
        "git", *args,
    ], cwd=root, text=True, capture_output=True, check=check)


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.com")


def _commit(root: Path, message: str) -> None:
    _git(root, "add", ".")
    _git(root, "commit", "-m", message)


TEST_MODULE = """import unittest


class SampleTests(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(True)
"""


class ReleaseOrchestratorTests(unittest.TestCase):
    def test_scan_reports_dirty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_sample.py").write_text(TEST_MODULE, encoding="utf-8")
            _commit(root, "feat: initial commit")
            (root / "dirty.txt").write_text("x\n", encoding="utf-8")

            result = main(["scan", "--root", str(root)])

            self.assertEqual(result, 1)

    def test_check_detects_clean_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_sample.py").write_text(TEST_MODULE, encoding="utf-8")
            _commit(root, "feat: initial commit")

            result = main(["check", "--root", str(root)])

            self.assertEqual(result, 0)

    def test_check_reports_dirty_and_secret_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_sample.py").write_text(TEST_MODULE, encoding="utf-8")
            _commit(root, "feat: initial commit")
            (root / ".env").write_text("SECRET=1\n", encoding="utf-8")

            result = main(["check", "--root", str(root), "--allow-dirty"])

            self.assertEqual(result, 1)

    def test_changelog_generates_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_sample.py").write_text(TEST_MODULE, encoding="utf-8")
            _commit(root, "feat: initial commit")
            _git(root, "tag", release_tag("0.1.0"))
            (root / "feature.txt").write_text("x\n", encoding="utf-8")
            _commit(root, "fix: patch issue")
            (root / "docs.txt").write_text("y\n", encoding="utf-8")
            _commit(root, "docs: update docs")

            result = main(["changelog", release_tag("0.1.0"), "--root", str(root), "--force"])

            self.assertEqual(result, 0)
            content = (root / "CHANGELOG.md").read_text(encoding="utf-8")
            self.assertIn("### fix", content)
            self.assertIn("### docs", content)

    def test_release_updates_version_and_tags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_sample.py").write_text(TEST_MODULE, encoding="utf-8")
            _commit(root, "feat: initial commit")
            _git(root, "tag", release_tag("0.1.0"))
            (root / "feature.txt").write_text("x\n", encoding="utf-8")
            _commit(root, "feat: add feature")

            result = main(["release", "0.2.0", "--root", str(root)])

            self.assertEqual(result, 0)
            self.assertEqual((root / "VERSION").read_text(encoding="utf-8").strip(), "0.2.0")
            self.assertTrue((root / "CHANGELOG.md").exists())
            self.assertEqual(_git(root, "tag", "--list", release_tag("0.2.0")).stdout.strip(), release_tag("0.2.0"))

    def test_release_dry_run_makes_no_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_sample.py").write_text(TEST_MODULE, encoding="utf-8")
            _commit(root, "feat: initial commit")
            _git(root, "tag", release_tag("0.1.0"))

            result = main(["release", "0.2.0", "--root", str(root), "--dry-run"])

            self.assertEqual(result, 0)
            self.assertFalse((root / "VERSION").exists())
            self.assertFalse((root / "CHANGELOG.md").exists())

    def test_rollback_requires_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_sample.py").write_text(
                "def test_ok():\n    assert True\n", encoding="utf-8")
            _commit(root, "feat: initial commit")
            _git(root, "tag", release_tag("0.1.0"))

            result = main(["rollback", "0.1.0", "--root", str(root)])

            self.assertEqual(result, 1)

    def test_rollback_deletes_only_tool_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_sample.py").write_text(
                "def test_ok():\n    assert True\n", encoding="utf-8")
            _commit(root, "feat: initial commit")
            _git(root, "tag", release_tag("0.1.0"))

            result = main(["rollback", "0.1.0", "--root", str(root), "--confirm"])

            self.assertEqual(result, 0)
            self.assertEqual(_git(root, "tag", "--list", release_tag("0.1.0")).stdout.strip(), "")

    def test_python_module_entry_point(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_sample.py").write_text(
                "def test_ok():\n    assert True\n", encoding="utf-8")
            _commit(root, "feat: initial commit")

            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            completed = subprocess.run(
                [sys.executable, "-m", "release_orchestrator", "check", "--root", str(root)],
                cwd=root,
                text=True,
                capture_output=True,
                env=env,
            )

            self.assertEqual(completed.returncode, 0)
