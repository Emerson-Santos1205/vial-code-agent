from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vial_code_agent.agent import GenerationResult
from vial_code_agent.cli import main
from vial_code_agent.core import VialCoreReference
from vial_code_agent.model import ModelResponse
from vial_code_agent.patches import PatchError
from vial_code_agent.session import SessionStore
from vial_code_agent.vial_runtime import VialRuntime


PATCH = """--- a/source.txt
+++ b/source.txt
@@ -1 +1 @@
-old
+new
"""

VENDOR = Path(__file__).resolve().parents[1] / "vendor" / "vial-core"


def _runtime(root: Path) -> VialRuntime:
    return VialRuntime(VialCoreReference(VENDOR), root / ".vial-state")


class _FakeToolResult:
    def __init__(self, status="SUCCESS", output=None, error=""):
        self.status = status
        self.output = output or {}
        self.error = error

    def ok(self) -> bool:
        return self.status == "SUCCESS"


class _FakeRuntime:
    def __init__(self) -> None:
        self.decision_trace_calls: list[str] = []
        self.invoke_tool_calls: list[tuple[str, dict]] = []
        self.apply_patch_result = _FakeToolResult()
        self.workspace_root: Path | None = None
        self._trace = {"id": "DEC-1"}
        self._snapshot = {"organization_id": "org-1"}

    def set_workspace_root(self, root: Path) -> None:
        self.workspace_root = root

    def snapshot(self) -> dict:
        return self._snapshot

    def decision_trace(self, decision_id: str) -> dict:
        self.decision_trace_calls.append(decision_id)
        if decision_id == "DEC-MISSING":
            raise KeyError(decision_id)
        return self._trace

    def invoke_tool(self, tool_id: str, arguments: dict, **kwargs):
        self.invoke_tool_calls.append((tool_id, arguments))
        if tool_id == "TOOL-RUN-TEST":
            return _FakeToolResult(status="REJECTED", error="tests rejected")
        return self.apply_patch_result

    def apply_patch(self, *args, **kwargs):
        return self.apply_patch_result

    def record_rollback(self, patch: str) -> None:
        return None

    def select_route(self, task: str, mode: str, deterministic: bool = False) -> str:
        return "advanced"


class CliIntegrationTests(unittest.TestCase):
    def test_review_validates_and_prints_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("old\n", encoding="utf-8")
            patch_file = root / "change.patch"
            patch_file.write_text(PATCH, encoding="utf-8")

            result = main(["--review", str(patch_file), "--root", str(root)])

            self.assertEqual(result, 0)

    def test_fake_provider_patch_is_applied_without_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("old\n", encoding="utf-8")
            generated = GenerationResult(ModelResponse("", 0), PATCH)

            class FakeAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            with patch("vial_code_agent.cli.CodeAgent", FakeAgent):
                result = main(
                    [
                        "--fix", "change old to new", "--root", str(root),
                        "--include", "source.txt",
                        "--test-command", sys.executable, "-c",
                        "import pathlib; assert pathlib.Path('source.txt').read_text() == 'new\\n'",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(source.read_text(encoding="utf-8"), "new\n")

    def test_failed_verification_rolls_back_and_records_compensation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("old\n", encoding="utf-8")
            generated = GenerationResult(ModelResponse("", 0), PATCH)

            class FakeAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            with patch("vial_code_agent.cli.CodeAgent", FakeAgent):
                result = main(
                    [
                        "--fix", "change old to new", "--root", str(root),
                        "--vial-root", str(VENDOR),
                        "--include", "source.txt",
                        "--test-command", sys.executable, "-c", "import sys; sys.exit(1)",
                    ]
                )

            self.assertEqual(result, 1)
            self.assertEqual(source.read_text(encoding="utf-8"), "old\n")
            runtime = _runtime(root)
            op_id = hashlib.sha256(PATCH.encode("utf-8")).hexdigest()
            self.assertIn(
                "ROLLBACK-" + op_id,
                [intent.operation_id for intent in runtime.coordinator.intents.values()],
            )

    def test_keep_on_failure_preserves_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("old\n", encoding="utf-8")
            generated = GenerationResult(ModelResponse("", 0), PATCH)

            class FakeAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            with patch("vial_code_agent.cli.CodeAgent", FakeAgent):
                result = main(
                    [
                        "--fix", "change old to new", "--root", str(root),
                        "--vial-root", str(VENDOR),
                        "--include", "source.txt",
                        "--keep-on-failure",
                        "--test-command", sys.executable, "-c", "import sys; sys.exit(1)",
                    ]
                )

            self.assertEqual(result, 1)
            self.assertEqual(source.read_text(encoding="utf-8"), "new\n")

    def test_trace_requires_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = main(["--trace", "DEC-0001", "--root", directory])
            self.assertEqual(result, 2)

    def test_status_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = main(["--status", "--root", directory])
            self.assertEqual(result, 1)

    def test_status_with_runtime_prints_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("vial_code_agent.cli._build_runtime") as build:
                build.return_value = _FakeRuntime()
                with patch("sys.stdout") as stdout:
                    result = main(["--status", "--root", str(root)])
            self.assertEqual(result, 0)

    def test_status_trace_ok_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("vial_code_agent.cli._build_runtime") as build:
                runtime = _FakeRuntime()
                build.return_value = runtime
                with patch("sys.stdout"):
                    ok = main(["--status", "--trace", "DEC-1", "--root", str(root)])
                with patch("sys.stdout"):
                    missing = main(
                        ["--status", "--trace", "DEC-MISSING", "--root", str(root)])
            self.assertEqual(ok, 0)
            self.assertEqual(missing, 1)
            self.assertEqual(runtime.decision_trace_calls, ["DEC-1", "DEC-MISSING"])

    def test_models_lists_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("vial_code_agent.cli.OpenCodeProvider") as provider_cls:
                provider = provider_cls.return_value
                provider.list_models.return_value = "openai/x\n"
                with patch("sys.stdout"):
                    result = main(["--models", "--root", directory])
                provider.list_models.side_effect = RuntimeError("down")
                with patch("sys.stderr"):
                    error = main(["--models", "--root", directory])
            self.assertEqual(result, 0)
            self.assertEqual(error, 1)

    def test_providers_lists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("vial_code_agent.cli.OpenCodeProvider") as provider_cls:
                provider_cls.return_value.list_providers.return_value = "openai\n"
                with patch("sys.stdout"):
                    result = main(["--providers", "--root", directory])
            self.assertEqual(result, 0)

    def test_review_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = main(
                ["--review", str(Path(directory) / "nope.patch"), "--root", directory])
            self.assertEqual(result, 1)

    def test_run_fallback_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = main(
                [
                    "--run", f"{sys.executable} -c \"import sys; sys.stdout.write('ran')\"",
                    "--root", directory,
                ]
            )
            self.assertEqual(result, 0)

    def test_run_rejected_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = main(["--run", "format-disk", "--root", directory])
            self.assertEqual(result, 2)

    def test_run_with_runtime_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("vial_code_agent.cli._build_runtime") as build:
                runtime = _FakeRuntime()
                runtime.apply_patch_result = _FakeToolResult(
                    status="REJECTED", error="not allowed")
                build.return_value = runtime
                with patch("sys.stderr"):
                    result = main(["--run", "format-disk", "--root", str(root)])
            self.assertEqual(result, 2)

    def test_fix_runtime_error_from_generate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("old\n", encoding="utf-8")

            class _BoomAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    raise RuntimeError("model unavailable")

            with patch("vial_code_agent.cli.CodeAgent", _BoomAgent):
                with patch("sys.stderr"):
                    result = main(
                        ["--fix", "change", "--root", str(root), "--include", "source.txt"])
            self.assertEqual(result, 2)

    def test_fix_no_patch_generated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("old\n", encoding="utf-8")
            generated = GenerationResult(ModelResponse("nothing", 0), None)

            class _NoPatchAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            with patch("vial_code_agent.cli.CodeAgent", _NoPatchAgent):
                with patch("sys.stderr"):
                    result = main(
                        ["--fix", "change", "--root", str(root), "--include", "source.txt"])
            self.assertEqual(result, 1)

    def test_fix_with_runtime_apply_patch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("old\n", encoding="utf-8")
            generated = GenerationResult(ModelResponse("", 0), PATCH)

            class _FakeAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            with patch("vial_code_agent.cli.CodeAgent", _FakeAgent):
                with patch("vial_code_agent.cli._build_runtime") as build:
                    runtime = _FakeRuntime()
                    runtime.apply_patch_result = _FakeToolResult(
                        status="FAILED", error="VIAL tool rejected patch")
                    build.return_value = runtime
                    with patch("sys.stderr"):
                        result = main(
                            [
                                "--fix", "change", "--root", str(root),
                                "--include", "source.txt",
                            ]
                        )
            self.assertEqual(result, 1)

    def test_tui_resume_unknown_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = main(["--session", "nope", "--root", directory])
            self.assertEqual(result, 2)

    def test_tui_starts_with_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("vial_code_agent.cli.VialTUI") as tui_cls:
                tui_cls.return_value.run.return_value = None
                result = main(["--prompt", "hi", "--root", str(root)])
            self.assertEqual(result, 0)

    def test_invalid_config_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".vial.json").write_text("{bad json", encoding="utf-8")
            with patch("sys.stderr"):
                result = main(["--status", "--root", str(root)])
            self.assertEqual(result, 2)

    def test_invalid_price_table_config_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".vial.json").write_text(
                json.dumps({"price_table_json": "{bad"}), encoding="utf-8")
            (root / "vendor" / "vial-core").mkdir(parents=True)
            with patch("sys.stderr"):
                result = main(["--status", "--root", str(root)])
            self.assertEqual(result, 2)

    def test_run_with_runtime_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("vial_code_agent.cli._build_runtime") as build:
                runtime = _FakeRuntime()
                runtime.apply_patch_result = _FakeToolResult(
                    output={"stdout": "ran\n", "stderr": "", "returncode": 0})
                build.return_value = runtime
                with patch("sys.stdout"):
                    result = main(["--run", "python -c print(1)", "--root", str(root)])
            self.assertEqual(result, 0)
            self.assertEqual(runtime.invoke_tool_calls[0][0], "TOOL-RUN-BUILD")

    def test_fix_workspace_changed_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("old\n", encoding="utf-8")
            generated = GenerationResult(
                ModelResponse("", 0), PATCH, workspace_changed=True)

            class _AppliedAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            with patch("vial_code_agent.cli.CodeAgent", _AppliedAgent):
                with patch("vial_code_agent.cli._build_runtime") as build:
                    build.return_value = _FakeRuntime()
                    with patch("sys.stdout"):
                        result = main(
                            [
                                "--fix", "change", "--root", str(root),
                                "--include", "source.txt",
                            ]
                        )
            self.assertEqual(result, 0)

    def test_fix_patch_validate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "other.txt").write_text("x\n", encoding="utf-8")
            generated = GenerationResult(ModelResponse("", 0), PATCH)

            class _FakeAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            with patch("vial_code_agent.cli.CodeAgent", _FakeAgent):
                with patch("sys.stderr"):
                    result = main(
                        [
                            "--fix", "change", "--root", str(root),
                            "--include", "other.txt",
                        ]
                    )
            self.assertEqual(result, 1)

    def test_verify_tests_rejected_by_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("old\n", encoding="utf-8")
            generated = GenerationResult(ModelResponse("", 0), PATCH)

            class _FakeAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            with patch("vial_code_agent.cli.CodeAgent", _FakeAgent):
                with patch("vial_code_agent.cli._build_runtime") as build:
                    runtime = _FakeRuntime()
                    runtime.apply_patch_result = _FakeToolResult()
                    build.return_value = runtime
                    with patch("sys.stdout"):
                        result = main(
                            [
                                "--fix", "change", "--root", str(root),
                                "--include", "source.txt",
                                "--test-command", sys.executable, "-c", "print(1)",
                            ]
                        )
            self.assertEqual(result, 1)
            self.assertEqual(runtime.invoke_tool_calls[-1][0], "TOOL-RUN-TEST")

    def test_verify_rollback_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("old\n", encoding="utf-8")
            generated = GenerationResult(ModelResponse("", 0), PATCH)

            class _FakeAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            with patch("vial_code_agent.cli.CodeAgent", _FakeAgent):
                with patch("vial_code_agent.cli._build_runtime") as build:
                    runtime = _FakeRuntime()
                    runtime.apply_patch_result = _FakeToolResult()
                    build.return_value = runtime
                    with patch("vial_code_agent.patches.PatchApplier.reverse",
                               side_effect=PatchError("cannot reverse")):
                        with patch("sys.stderr"):
                            result = main(
                                [
                                    "--fix", "change", "--root", str(root),
                                    "--include", "source.txt",
                                    "--test-command", sys.executable,
                                    "-c", "import sys; sys.exit(1)",
                                ]
                            )
            self.assertEqual(result, 1)

    def test_tui_resume_existing_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / ".vial-sessions")
            session_id = store.create()
            with patch("vial_code_agent.cli.VialTUI") as tui_cls:
                tui_cls.return_value.run.return_value = None
                result = main(["--session", session_id, "--root", str(root)])
            self.assertEqual(result, 0)

    def test_tui_continue_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root / ".vial-sessions")
            store.create()
            with patch("vial_code_agent.cli.VialTUI") as tui_cls:
                tui_cls.return_value.run.return_value = None
                result = main(["--continue", "--root", str(root)])
            self.assertEqual(result, 0)

    def test_tui_with_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("vial_code_agent.cli._build_runtime") as build:
                runtime = _FakeRuntime()
                build.return_value = runtime
                with patch("vial_code_agent.cli.VialTUI") as tui_cls:
                    tui_cls.return_value.run.return_value = None
                    result = main(["--prompt", "hi", "--root", str(root)])
            self.assertEqual(result, 0)
            self.assertEqual(runtime.workspace_root, root.resolve())

    def test_review_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("old\n", encoding="utf-8")
            (root / "change.patch").write_text(PATCH, encoding="utf-8")
            with patch("vial_code_agent.cli.Path.cwd", return_value=root):
                result = main(["--review", "change.patch", "--root", str(root)])
            self.assertEqual(result, 0)

    def test_run_with_runtime_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("vial_code_agent.cli._build_runtime") as build:
                runtime = _FakeRuntime()
                runtime.apply_patch_result = _FakeToolResult(
                    output={"stdout": "", "stderr": "boom", "returncode": 3})
                build.return_value = runtime
                with patch("sys.stderr"):
                    result = main(["--run", "python -c print(1)", "--root", str(root)])
            self.assertEqual(result, 3)

    def test_run_fallback_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fail.py"
            script.write_text(
                "import sys\nsys.stderr.write('oops')\nsys.exit(2)\n",
                encoding="utf-8")
            with patch("sys.stderr"):
                result = main(
                    ["--run", f"{sys.executable} fail.py", "--root", str(root)])
            self.assertEqual(result, 2)

    def test_fix_explicit_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("old\n", encoding="utf-8")
            generated = GenerationResult(ModelResponse("", 0), PATCH)

            class _FakeAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            with patch("vial_code_agent.cli.CodeAgent", _FakeAgent):
                with patch("sys.stdout"):
                    result = main(
                        [
                            "--fix", "change", "--root", str(root),
                            "--model", "openai/gpt-5.6-luna-fast",
                            "--include", "source.txt",
                        ]
                    )
            self.assertEqual(result, 0)

    def test_fix_with_runtime_invalid_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".vial.json").write_text(
                json.dumps({"price_table_json": "{bad"}), encoding="utf-8")
            (root / "vendor" / "vial-core").mkdir(parents=True)
            (root / "source.txt").write_text("old\n", encoding="utf-8")
            with patch("sys.stderr"):
                result = main(
                    [
                        "--fix", "change", "--root", str(root),
                        "--include", "source.txt",
                    ]
                )
            self.assertEqual(result, 2)

    def test_reconfigure_error_is_ignored(self) -> None:
        class _NoReconfigure:
            def write(self, text):
                return None

            def flush(self):
                return None

            def reconfigure(self, **kwargs):
                raise ValueError("cannot reconfigure")

        with tempfile.TemporaryDirectory() as directory:
            with patch("vial_code_agent.cli.sys.stdout", _NoReconfigure()), \
                 patch("vial_code_agent.cli.sys.stderr", _NoReconfigure()):
                result = main(["--status", "--root", directory])
            self.assertEqual(result, 1)

    def test_fix_runtime_build_fails_inside_run_fix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("old\n", encoding="utf-8")

            class _FakeAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    raise AssertionError("should not be called")

            calls = {"n": 0}

            def _build(root, config, vial):
                calls["n"] += 1
                if calls["n"] == 1:
                    return _FakeRuntime()
                raise ValueError("bad runtime")

            with patch("vial_code_agent.cli.CodeAgent", _FakeAgent):
                with patch("vial_code_agent.cli._build_runtime", side_effect=_build):
                    with patch("sys.stderr"):
                        result = main(
                            ["--fix", "change", "--root", str(root),
                             "--include", "source.txt"])
            self.assertEqual(result, 2)

    def test_verify_rollback_failure_with_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("old\n", encoding="utf-8")
            generated = GenerationResult(ModelResponse("", 0), PATCH)

            class _FakeAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            class _TestsFailRuntime(_FakeRuntime):
                def invoke_tool(self, tool_id, arguments, **kwargs):
                    self.invoke_tool_calls.append((tool_id, arguments))
                    return _FakeToolResult(
                        output={"returncode": 1, "stdout": "out", "stderr": "err"})

            with patch("vial_code_agent.cli.CodeAgent", _FakeAgent):
                with patch("vial_code_agent.cli._build_runtime") as build:
                    runtime = _TestsFailRuntime()
                    build.return_value = runtime
                    with patch("vial_code_agent.patches.PatchApplier.reverse",
                               side_effect=PatchError("cannot reverse")):
                        with patch("sys.stdout"):
                            with patch("sys.stderr"):
                                result = main(
                                    [
                                        "--fix", "change", "--root", str(root),
                                        "--include", "source.txt",
                                        "--test-command", sys.executable,
                                        "-c", "import sys; sys.exit(1)",
                                    ]
                                )
            self.assertEqual(result, 1)

    def test_verify_fallback_failure_with_stdout_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source.txt").write_text("old\n", encoding="utf-8")
            generated = GenerationResult(ModelResponse("", 0), PATCH)

            class _FakeAgent:
                def __init__(self, provider: object, runtime=None) -> None:
                    pass

                def generate(self, *args: object, **kwargs: object) -> GenerationResult:
                    return generated

            with patch("vial_code_agent.cli.CodeAgent", _FakeAgent):
                with patch("sys.stdout"):
                    with patch("sys.stderr"):
                        result = main(
                            [
                                "--fix", "change", "--root", str(root),
                                "--include", "source.txt",
                                "--test-command", sys.executable,
                                "-c",
                                "import sys; sys.stdout.write('out'); sys.stderr.write('err'); sys.exit(1)",
                            ]
                        )
            self.assertEqual(result, 1)
            self.assertEqual((root / "source.txt").read_text(encoding="utf-8"), "old\n")
