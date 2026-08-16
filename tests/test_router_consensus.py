from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vial_code_agent.model import ModelResponse
from vial_code_agent.router import RoutingGraph
from vial_code_agent.servers import ServerRegistry


def _graph(tmp: str, pool: list[str] | None = None) -> RoutingGraph:
    registry = ServerRegistry(Path(tmp))
    for model in pool or []:
        registry.pool_add(model)
    return RoutingGraph(registry)


class DispatchConsensusTests(unittest.TestCase):
    def test_agreement_when_models_converge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = _graph(directory, pool=["a/reasoning", "b/reasoning"])

            class Fake:
                def __init__(self, model_ref: str = "", *args: object,
                             **kwargs: object) -> None:
                    pass

                def chat(self, prompt: str, root: Path | None = None,
                         history: object = None) -> ModelResponse:
                    return ModelResponse("def add(a, b):\n    return a + b\n", 0)

            with patch("vial_code_agent.router.OpenCodeProvider", Fake):
                result, decision = graph.dispatch_consensus("implement add()")

            self.assertTrue(result.agreed)
            self.assertGreaterEqual(result.agreement_ratio, 0.6)
            self.assertEqual(len(result.responses), 2)
            self.assertIn("consensus=True", decision.note)

    def test_disagreement_when_models_diverge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = _graph(directory, pool=["a/reasoning", "b/reasoning"])
            answers = iter([
                "def add(a, b):\n    return a + b\n",
                "DROP TABLE users; -- totally unrelated output\n",
            ])

            class Fake:
                def __init__(self, model_ref: str = "", *args: object,
                             **kwargs: object) -> None:
                    pass

                def chat(self, prompt: str, root: Path | None = None,
                         history: object = None) -> ModelResponse:
                    return ModelResponse(next(answers), 0)

            with patch("vial_code_agent.router.OpenCodeProvider", Fake):
                result, decision = graph.dispatch_consensus(
                    "implement add()", min_agreement=0.6)

            self.assertFalse(result.agreed)
            self.assertLess(result.agreement_ratio, 0.6)
            # Disagreement must not hide the losing answer: both raw
            # responses stay available for human review.
            self.assertEqual(len(result.responses), 2)

    def test_single_valid_candidate_never_claims_consensus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = _graph(directory, pool=["only/model"])

            class Fake:
                def __init__(self, model_ref: str = "", *args: object,
                             **kwargs: object) -> None:
                    pass

                def chat(self, prompt: str, root: Path | None = None,
                         history: object = None) -> ModelResponse:
                    return ModelResponse("ok", 0)

            with patch("vial_code_agent.router.OpenCodeProvider", Fake):
                result, decision = graph.dispatch_consensus("implement x")

            self.assertFalse(result.agreed)
            self.assertEqual(result.agreement_ratio, 0.0)
            self.assertEqual(decision.note, "insufficient candidates for consensus")

    def test_no_candidates_reports_failure_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = _graph(directory, pool=[])
            result, decision = graph.dispatch_consensus("implement x")
            self.assertFalse(result.agreed)
            self.assertEqual(decision.note, "all consensus candidates failed")

    def test_quorum_caps_number_of_models_consulted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            graph = _graph(
                directory, pool=["a/reasoning", "b/reasoning", "c/reasoning"])
            calls: list[str] = []

            class Fake:
                def __init__(self, model_ref: str = "", *args: object,
                             **kwargs: object) -> None:
                    self.model_ref = model_ref

                def chat(self, prompt: str, root: Path | None = None,
                         history: object = None) -> ModelResponse:
                    calls.append(self.model_ref)
                    return ModelResponse("same answer", 0)

            with patch("vial_code_agent.router.OpenCodeProvider", Fake):
                graph.dispatch_consensus("implement x", quorum=2)

            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
