from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vial_code_agent.model import ModelResponse
from vial_code_agent.router import RoutingGraph
from vial_code_agent.servers import ServerRegistry


class RoutingGraphTests(unittest.TestCase):
    def _graph(self, tmp: str, pool: list[str] | None = None,
               default_model: str = "auto") -> RoutingGraph:
        registry = ServerRegistry(Path(tmp))
        for model in pool or []:
            registry.pool_add(model)
        return RoutingGraph(registry, default_model=default_model)

    def test_analyze_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory)
            decision = graph.analyze("trim trailing whitespace")
            self.assertEqual(decision.tier, "deterministic")
            self.assertEqual(decision.deterministic_keyword, "trim trailing whitespace")

    def test_analyze_light(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory)
            self.assertEqual(graph.analyze("explain this module").tier, "light")
            self.assertEqual(graph.analyze("hi there").tier, "light")

    def test_analyze_advanced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory)
            self.assertEqual(graph.analyze("implement persistence").tier, "advanced")

    def test_candidates_light_prefers_fast(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(
                directory,
                pool=["openai/gpt-5.6-luna", "openai/gpt-5.6-luna-fast"],
            )
            decision = graph.analyze("explain this")
            candidates = graph.candidates(decision)
            self.assertEqual(candidates[0], "openai/gpt-5.6-luna-fast")

    def test_candidates_empty_pool_falls_back_to_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory, default_model="openai/gpt-5.6-luna")
            candidates = graph.candidates(graph.analyze("implement x"))
            self.assertEqual(candidates, ["openai/gpt-5.6-luna"])

    def test_model_for_explicit_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory, pool=["fast/model", "slow/model"])
            self.assertEqual(
                graph.model_for("explain x", requested_model="slow/model"),
                "slow/model",
            )

    def test_dispatch_deterministic_no_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory, pool=["openai/gpt-5.6-luna"])
            with patch("vial_code_agent.router.OpenCodeProvider") as provider:
                response, decision = graph.dispatch("trim trailing whitespace")
            provider.assert_not_called()
            self.assertEqual(decision.tier, "deterministic")
            self.assertEqual(response.returncode, 0)

    def test_dispatch_parallel_picks_first_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory, pool=["a/fast", "b/fast"])

            class Fake:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                def chat(self, prompt: str, root: Path | None = None) -> ModelResponse:
                    return ModelResponse("answer from a", 0)

            with patch("vial_code_agent.router.OpenCodeProvider", Fake):
                response, decision = graph.dispatch("explain the parser")
            self.assertEqual(decision.model, "a/fast")
            self.assertIn("answer from a", response.text)

    def test_dispatch_forwards_history_to_pinned_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory, pool=["a/fast"])
            recorded: dict[str, object] = {}

            class Rec:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                def chat(self, prompt: str, root: Path | None = None,
                         history: object = None) -> ModelResponse:
                    recorded["history"] = history
                    return ModelResponse("ok", 0)

            with patch("vial_code_agent.router.OpenCodeProvider", Rec):
                graph.dispatch(
                    "explain x", requested_model="m1",
                    history=[("user", "hi"), ("assistant", "oi")],
                )
            self.assertEqual(
                recorded["history"], [("user", "hi"), ("assistant", "oi")])

    def test_dispatch_forwards_history_in_parallel_pool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory, pool=["a/fast", "b/fast"])
            received: list[object] = []

            class Rec:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                def chat(self, prompt: str, root: Path | None = None,
                         history: object = None) -> ModelResponse:
                    received.append(history)
                    return ModelResponse("ok", 0)

            with patch("vial_code_agent.router.OpenCodeProvider", Rec):
                graph.dispatch(
                    "explain the parser", history=[("user", "hello")])
            self.assertEqual(received, [[("user", "hello")], [("user", "hello")]])

    def test_dispatch_all_failed_returns_first_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory, pool=["a/fast", "b/fast"])

            class Failing:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                def chat(self, prompt: str, root: Path | None = None) -> ModelResponse:
                    return ModelResponse("", 1, stderr="boom")

            with patch("vial_code_agent.router.OpenCodeProvider", Failing):
                response, decision = graph.dispatch("explain the parser")
            self.assertEqual(response.returncode, 1)

    def test_dispatch_escalates_tier_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(
                directory,
                pool=["a/fast", "b/reasoning", "c/reasoning"],
            )
            calls: list[str] = []

            class Escalating:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    self.model = str(args[0]) if args else ""

                def chat(self, prompt: str, root: Path | None = None) -> ModelResponse:
                    calls.append(self.model)
                    if "fast" in self.model:
                        return ModelResponse("", 1, stderr="light failed")
                    return ModelResponse("answer from reasoning", 0)

            with patch("vial_code_agent.router.OpenCodeProvider", Escalating):
                response, decision = graph.dispatch("implement the feature")
            self.assertEqual(decision.model, "b/reasoning")
            self.assertIn("answer from reasoning", response.text)
            self.assertEqual(calls, ["a/fast", "b/reasoning", "c/reasoning"])

    def test_dispatch_light_tier_only_invokes_light_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(
                directory,
                pool=["a/fast", "b/reasoning"],
            )
            calls: list[str] = []

            class Tiered:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    self.model = str(args[0]) if args else ""

                def chat(self, prompt: str, root: Path | None = None) -> ModelResponse:
                    calls.append(self.model)
                    return ModelResponse("ok", 0)

            with patch("vial_code_agent.router.OpenCodeProvider", Tiered):
                response, decision = graph.dispatch("explain the parser")
            self.assertEqual(calls, ["a/fast"])
            self.assertEqual(decision.model, "a/fast")

    def test_dispatch_stream_pinned_yields_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory, pool=["a/fast"])

            class Streamer:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                def chat_stream(self, prompt: str, root: Path | None = None,
                                history: object = None):
                    yield from ("one ", "two")

            with patch("vial_code_agent.router.OpenCodeProvider", Streamer):
                chunks = list(graph.dispatch_stream(
                    "explain x", requested_model="m1",
                    history=[("user", "hi")]))
            self.assertEqual("".join(chunks), "one two")

    def test_dispatch_stream_deterministic_yields_plain_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory, pool=["a/fast"])
            with patch("vial_code_agent.router.OpenCodeProvider") as provider:
                chunks = list(graph.dispatch_stream("trim trailing whitespace"))
            provider.assert_not_called()
            self.assertEqual(chunks, ["deterministic: trim trailing whitespace"])

    def test_dispatch_stream_auto_falls_back_to_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory, pool=["a/fast"])

            class Fake:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                def chat(self, prompt: str, root: Path | None = None) -> ModelResponse:
                    return ModelResponse("pooled answer", 0)

            with patch("vial_code_agent.router.OpenCodeProvider", Fake):
                chunks = list(graph.dispatch_stream("explain the parser"))
            self.assertEqual("".join(chunks), "pooled answer")

    def test_dispatch_stream_auto_reports_empty_model_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory, pool=["a/fast"])

            class Fake:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                def chat(self, prompt: str, root: Path | None = None) -> ModelResponse:
                    return ModelResponse("", 1, stderr="provider unavailable")

            with patch("vial_code_agent.router.OpenCodeProvider", Fake):
                chunks = list(graph.dispatch_stream("build the release tool"))
            self.assertEqual(
                chunks,
                ["error: model exited with code 1: provider unavailable"],
            )

    def test_dispatch_stream_pinned_reports_empty_model_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory, pool=["a/fast"])

            class Fake:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    self.last_response = ModelResponse("", 1, stderr="provider failed")

                def chat_stream(self, prompt: str, root: Path | None = None,
                                history: object = None):
                    if False:
                        yield "unreachable"

            with patch("vial_code_agent.router.OpenCodeProvider", Fake):
                chunks = list(graph.dispatch_stream(
                    "build the release tool", requested_model="m1"))
            self.assertEqual(
                chunks,
                ["error: model exited with code 1: provider failed"],
            )

    def test_cancel_active_terminates_streaming_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = self._graph(directory, pool=["a/fast"])
            cancelled = []

            class Cancellable:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                def chat_stream(self, prompt: str, root: Path | None = None,
                                history: object = None):
                    yield "pending"

                def cancel(self) -> None:
                    cancelled.append(True)

            with patch("vial_code_agent.router.OpenCodeProvider", Cancellable):
                iterator = graph.dispatch_stream("explain x", requested_model="m1")
                next(iterator)
                graph.cancel_active()
            self.assertEqual(cancelled, [True])
            graph.cancel_active()  # no-op with no active provider


class AgreementRatioTests(unittest.TestCase):
    """Tests for _agreement_ratio with apply-and-compare conflict detection."""

    PATCH_TEMPLATE = (
        "diff --git a/file.txt b/file.txt\n"
        "--- a/file.txt\n"
        "+++ b/file.txt\n"
        "@@ -1,3 +1,3 @@\n"
        " line1\n"
        "-old\n"
        "+{new}\n"
        " line3\n"
    )

    def test_identical_patches_yield_one(self) -> None:
        from vial_code_agent.model import ModelResponse
        from vial_code_agent.router import _agreement_ratio
        patch = self.PATCH_TEMPLATE.format(new="same")
        a = ModelResponse(text=f"some prose\n\n{patch}", returncode=0)
        b = ModelResponse(text=f"different prose\n\n{patch}", returncode=0)
        ratio = _agreement_ratio(a, b)
        self.assertEqual(ratio, 1.0)

    def test_different_patches_yield_zero(self) -> None:
        from vial_code_agent.model import ModelResponse
        from vial_code_agent.router import _agreement_ratio
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "file.txt").write_text("line1\nold\nline3\n")
            patch_a = self.PATCH_TEMPLATE.format(new="alpha")
            patch_b = self.PATCH_TEMPLATE.format(new="beta")
            a = ModelResponse(text=patch_a, returncode=0)
            b = ModelResponse(text=patch_b, returncode=0)
            ratio = _agreement_ratio(a, b, root=root)
            self.assertEqual(ratio, 0.0)

    def test_apply_and_compare_with_workspace(self) -> None:
        from vial_code_agent.model import ModelResponse
        from vial_code_agent.router import _agreement_ratio
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "file.txt").write_text("line1\nold\nline3\n")
            patch_a = self.PATCH_TEMPLATE.format(new="from_a")
            patch_b = self.PATCH_TEMPLATE.format(new="from_b")
            a = ModelResponse(text=patch_a, returncode=0)
            b = ModelResponse(text=patch_b, returncode=0)
            ratio = _agreement_ratio(a, b, root=root)
            self.assertEqual(ratio, 0.0)

    def test_apply_and_compare_identical_with_workspace(self) -> None:
        from vial_code_agent.model import ModelResponse
        from vial_code_agent.router import _agreement_ratio
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "file.txt").write_text("line1\nold\nline3\n")
            patch = self.PATCH_TEMPLATE.format(new="same")
            a = ModelResponse(text=patch, returncode=0)
            b = ModelResponse(text=patch, returncode=0)
            ratio = _agreement_ratio(a, b, root=root)
            self.assertEqual(ratio, 1.0)

    def test_fallback_to_text_when_no_patches(self) -> None:
        from vial_code_agent.model import ModelResponse
        from vial_code_agent.router import _agreement_ratio
        a = ModelResponse(text="hello world", returncode=0)
        b = ModelResponse(text="hello world", returncode=0)
        ratio = _agreement_ratio(a, b)
        self.assertEqual(ratio, 1.0)


if __name__ == "__main__":
    unittest.main()
