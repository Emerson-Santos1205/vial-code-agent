from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from release_orchestrator.cli import main
from release_orchestrator.core import (
    Commit,
    calculate_release_changes,
    release_tag,
    repo_health_issues,
    validate_release_transition,
    validate_rollback_transition,
)
from release_orchestrator.domain import categorize_commits, validate_semver
from release_orchestrator.storage import atomic_write_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_SRC = PROJECT_ROOT / "src"

PASSING_TESTS = """import unittest


class SampleTests(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(True)
"""

FAILING_TESTS = """import unittest


class SampleTests(unittest.TestCase):
    def test_fail(self):
        self.assertTrue(False)
"""


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=check)


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.com")


def _commit(root: Path, message: str) -> None:
    _git(root, "add", ".")
    _git(root, "commit", "-m", message)


def _write_basic_project(root: Path, *, tests: str = PASSING_TESTS) -> None:
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    tests_dir = root / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_sample.py").write_text(tests, encoding="utf-8")
    vendor_dir = root / "vendor" / "vial-core"
    vendor_dir.mkdir(parents=True, exist_ok=True)
    (vendor_dir / ".gitkeep").write_text("", encoding="utf-8")


def _write_release_commits(root: Path) -> None:
    (root / "feature.txt").write_text("feat\n", encoding="utf-8")
    _commit(root, "feat: add feature")
    (root / "bugfix.txt").write_text("fix\n", encoding="utf-8")
    _commit(root, "fix(parser): repair bug")
    (root / "docs.txt").write_text("docs\n", encoding="utf-8")
    _commit(root, "docs: update docs")
    (root / "test.txt").write_text("test\n", encoding="utf-8")
    _commit(root, "test: expand coverage")
    (root / "misc.txt").write_text("misc\n", encoding="utf-8")
    _commit(root, "chore: misc cleanup")


def _capture_main(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _run_module(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_SRC)
    return subprocess.run(
        [sys.executable, "-m", "release_orchestrator", *args],
        cwd=root,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


class ReleaseOrchestratorTests(unittest.TestCase):
    def test_semver_validation(self) -> None:
        self.assertTrue(validate_semver("0.1.0"))
        self.assertTrue(validate_semver("12.34.56"))
        for version in ("1", "1.2", "1.2.3.4", "01.2.3", "1.02.3", "1.2.03", "v1.2.3"):
            self.assertFalse(validate_semver(version))

    def test_commit_categorization(self) -> None:
        commits = [
            Commit("aaaaaaa", "feat: new thing"),
            Commit("bbbbbbb", "fix(parser): bug fix"),
            Commit("ccccccc", "docs: update docs"),
            Commit("ddddddd", "test: add coverage"),
            Commit("eeeeeee", "feat!: breaking change"),
            Commit("fffffff", "misc change"),
        ]

        grouped = categorize_commits(commits)
        self.assertEqual([commit.sha for commit in grouped["feat"]], ["aaaaaaa", "eeeeeee"])
        self.assertEqual([commit.sha for commit in grouped["fix"]], ["bbbbbbb"])
        self.assertEqual([commit.sha for commit in grouped["docs"]], ["ccccccc"])
        self.assertEqual([commit.sha for commit in grouped["test"]], ["ddddddd"])
        self.assertEqual([commit.sha for commit in grouped["other"]], ["fffffff"])
        self.assertEqual(
            [commit.sha for commit in categorize_commits([Commit("eeeeeee", "feat!: breaking change")])["feat"]],
            ["eeeeeee"],
        )

        changes = calculate_release_changes("0.2.0", commits, "release-orchestrator-v0.1.0")
        self.assertIn("### feat", changes.content)
        self.assertIn("### other", changes.content)
        self.assertEqual(changes.counts["feat"], 2)

    def test_transition_validators(self) -> None:
        release_issues = validate_release_transition(
            version="1.2.3",
            repo_issues=["missing README.md"],
            tests_ok=False,
            tag_exists=True,
            confirm=False,
        )
        self.assertIn("missing README.md", release_issues)
        self.assertIn("release requires --confirm", release_issues)
        self.assertIn("test suite failed", release_issues)
        self.assertIn("tag release-orchestrator-v1.2.3 already exists", release_issues)

        rollback_issues = validate_rollback_transition(
            version="1.2.3",
            confirm=False,
            tag_exists=False,
            dry_run=False,
        )
        self.assertIn("rollback requires --confirm", rollback_issues)
        self.assertIn("tag release-orchestrator-v1.2.3 not found", rollback_issues)

    def test_repo_health_issues_and_allow_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_basic_project(root)
            _commit(root, "feat: initial commit")
            (root / "dirty.txt").write_text("x\n", encoding="utf-8")

            issues = repo_health_issues(root)
            self.assertIn("working tree is dirty", issues)
            self.assertNotIn("working tree is dirty", repo_health_issues(root, allow_dirty=True))

    def test_atomic_write_uses_replace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "output.txt"
            calls: list[tuple[Path, Path]] = []
            original_replace = os.replace

            def _record_replace(src: str, dst: str) -> None:
                calls.append((Path(src), Path(dst)))
                original_replace(src, dst)

            with mock.patch("release_orchestrator.storage.os.replace", side_effect=_record_replace):
                atomic_write_text(target, "hello\n")

            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], target)
            self.assertNotEqual(calls[0][0], target)

    def test_scan_json_reports_dirty_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_basic_project(root)
            _commit(root, "feat: initial commit")

            code, stdout, stderr = _capture_main(["scan", "--root", str(root), "--json"])
            payload = json.loads(stdout)

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertFalse(payload["dirty"])
            self.assertEqual(payload["modified_files"], [])

            (root / "dirty.txt").write_text("x\n", encoding="utf-8")
            code, stdout, _ = _capture_main(["scan", "--root", str(root), "--json"])
            payload = json.loads(stdout)

            self.assertEqual(code, 1)
            self.assertTrue(payload["dirty"])
            self.assertIn("dirty.txt", payload["modified_files"])

    def test_check_json_reports_clean_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_basic_project(root)
            _commit(root, "feat: initial commit")

            code, stdout, stderr = _capture_main(["check", "--root", str(root), "--json"])
            payload = json.loads(stdout)

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(payload["issues"], [])
            self.assertTrue(payload["tests_passed"])

    def test_check_reports_failing_suite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_basic_project(root, tests=FAILING_TESTS)
            _commit(root, "feat: initial commit")

            code, stdout, stderr = _capture_main(["check", "--root", str(root)])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("test suite failed", stderr)

    def test_check_reports_secret_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_basic_project(root)
            (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
            _commit(root, "feat: initial commit")

            code, _, stderr = _capture_main(["check", "--root", str(root), "--allow-dirty"])

            self.assertEqual(code, 1)
            self.assertIn("secret files present", stderr)

    def test_changelog_json_and_force_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_basic_project(root)
            _commit(root, "feat: initial commit")
            _git(root, "tag", release_tag("0.1.0"))
            (root / "feature.txt").write_text("x\n", encoding="utf-8")
            _commit(root, "feat: add feature")

            code, stdout, stderr = _capture_main([
                "changelog",
                release_tag("0.1.0"),
                "--root",
                str(root),
                "--json",
                "--force",
            ])
            payload = json.loads(stdout)

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertTrue((root / "CHANGELOG.md").exists())
            self.assertEqual(payload["counts"]["feat"], 1)

            code, _, stderr = _capture_main([
                "changelog",
                release_tag("0.1.0"),
                "--root",
                str(root),
            ])
            self.assertEqual(code, 1)
            self.assertIn("CHANGELOG.md already exists", stderr)

    def test_changelog_reports_missing_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_basic_project(root)
            _commit(root, "feat: initial commit")

            code, _, stderr = _capture_main([
                "changelog",
                release_tag("0.1.0"),
                "--root",
                str(root),
            ])

            self.assertEqual(code, 1)
            self.assertIn("tag release-orchestrator-v0.1.0 not found", stderr)

    def test_changelog_force_does_not_ignore_missing_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_basic_project(root)
            _commit(root, "feat: initial commit")

            code, _, stderr = _capture_main([
                "changelog",
                release_tag("0.1.0"),
                "--root",
                str(root),
                "--force",
            ])

            self.assertEqual(code, 1)
            self.assertIn("tag release-orchestrator-v0.1.0 not found", stderr)

    def test_release_dry_run_makes_no_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_basic_project(root)
            _commit(root, "feat: initial commit")
            _git(root, "tag", release_tag("0.1.0"))
            _write_release_commits(root)

            code, stdout, stderr = _capture_main(["release", "0.2.0", "--root", str(root), "--dry-run", "--confirm"])

            self.assertEqual(code, 0)
            self.assertIn("dry-run: would write VERSION=0.2.0", stdout)
            self.assertEqual(stderr, "")
            self.assertFalse((root / "VERSION").exists())
            self.assertFalse((root / "CHANGELOG.md").exists())
            self.assertEqual(_git(root, "tag", "--list", release_tag("0.2.0")).stdout.strip(), "")

    def test_release_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_basic_project(root)
            _commit(root, "feat: initial commit")
            _git(root, "tag", release_tag("0.1.0"))
            _write_release_commits(root)

            code, _, stderr = _capture_main(["release", "0.2.0", "--root", str(root)])

            self.assertEqual(code, 1)
            self.assertIn("release requires --confirm", stderr)
            self.assertFalse((root / "VERSION").exists())
            self.assertEqual(_git(root, "tag", "--list", release_tag("0.2.0")).stdout.strip(), "")

    def test_release_confirm_allows_tag_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_basic_project(root)
            _commit(root, "feat: initial commit")
            _git(root, "tag", release_tag("0.1.0"))
            _write_release_commits(root)

            code, stdout, stderr = _capture_main(["release", "0.2.0", "--root", str(root), "--confirm"])

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("released 0.2.0", stdout)
            self.assertEqual((root / "VERSION").read_text(encoding="utf-8"), "0.2.0\n")
            self.assertEqual(_git(root, "tag", "--list", release_tag("0.2.0")).stdout.strip(), release_tag("0.2.0"))

    def test_release_and_rollback_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_basic_project(root)
            _commit(root, "feat: initial commit")
            _git(root, "tag", release_tag("0.1.0"))
            _write_release_commits(root)

            code, _, stderr = _capture_main(["rollback", "0.1.0", "--root", str(root)])
            self.assertEqual(code, 1)
            self.assertIn("rollback requires --confirm", stderr)

            code, _, stderr = _capture_main(["rollback", "0.1.0", "--root", str(root), "--confirm"])
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertEqual(_git(root, "tag", "--list", release_tag("0.1.0")).stdout.strip(), "")

    def test_rollback_dry_run_preserves_tag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_basic_project(root)
            _commit(root, "feat: initial commit")
            _git(root, "tag", release_tag("0.1.0"))

            code, stdout, stderr = _capture_main(["rollback", "0.1.0", "--root", str(root), "--dry-run"])

            self.assertEqual(code, 0)
            self.assertIn("dry-run: would delete tag", stdout)
            self.assertEqual(stderr, "")
            self.assertEqual(_git(root, "tag", "--list", release_tag("0.1.0")).stdout.strip(), release_tag("0.1.0"))

    def test_full_integration_via_module_entry_point(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _init_repo(root)
            _write_basic_project(root)
            _commit(root, "feat: initial commit")
            _git(root, "tag", release_tag("0.1.0"))
            _write_release_commits(root)

            scan = _run_module(root, "scan", "--json")
            self.assertEqual(scan.returncode, 0)
            self.assertFalse(json.loads(scan.stdout)["dirty"])

            check = _run_module(root, "check", "--json")
            self.assertEqual(check.returncode, 0)
            self.assertEqual(json.loads(check.stdout)["issues"], [])

            release = _run_module(root, "release", "0.2.0", "--confirm")
            self.assertEqual(release.returncode, 0)
            self.assertEqual((root / "VERSION").read_text(encoding="utf-8").strip(), "0.2.0")
            changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
            self.assertIn("### feat", changelog)
            self.assertIn("### fix", changelog)
            self.assertIn("### docs", changelog)
            self.assertIn("### test", changelog)
            self.assertIn("### other", changelog)
            self.assertEqual(_git(root, "tag", "--list", release_tag("0.2.0")).stdout.strip(), release_tag("0.2.0"))

            dirty_scan = _run_module(root, "scan", "--json")
            self.assertEqual(dirty_scan.returncode, 1)
            self.assertTrue(json.loads(dirty_scan.stdout)["dirty"])

            rollback = _run_module(root, "rollback", "0.2.0", "--confirm")
            self.assertEqual(rollback.returncode, 0)
            self.assertEqual(_git(root, "tag", "--list", release_tag("0.2.0")).stdout.strip(), "")
