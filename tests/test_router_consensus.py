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
        """For N>2 models, all are consulted for clustering. Quorum only
        affects the minimum cluster size required for consensus."""
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

            # All 3 models are consulted for proper clustering
            self.assertEqual(len(calls), 3)

    def test_three_models_majority_cluster(self) -> None:
        """Three models: two agree, one diverges. Majority cluster wins."""
        with tempfile.TemporaryDirectory() as directory:
            graph = _graph(
                directory,
                pool=["a/reasoning", "b/reasoning", "c/reasoning"])
            answers = iter([
                "def add(a, b):\n    return a + b\n",
                "def add(a, b):\n    return a + b\n",
                "print('hello world')",
            ])

            class Fake:
                def __init__(self, model_ref: str = "", *args: object,
                             **kwargs: object) -> None:
                    pass

                def chat(self, prompt: str, root: Path | None = None,
                         history: object = None) -> ModelResponse:
                    return ModelResponse(next(answers), 0)

            with patch("vial_code_agent.router.OpenCodeProvider", Fake):
                # quorum=2: majority cluster (2 models) meets quorum
                result, decision = graph.dispatch_consensus(
                    "implement add()", quorum=2, min_agreement=0.6)

            self.assertTrue(result.agreed)
            self.assertGreaterEqual(result.agreement_ratio, 0.6)
            self.assertEqual(len(result.responses), 3)
            # Clusters should be present for N>2
            self.assertEqual(len(result.clusters), 2)
            # Largest cluster should have 2 members
            self.assertEqual(len(result.clusters[0]), 2)

    def test_three_models_no_quorum(self) -> None:
        """Three models all disagree: no cluster meets quorum."""
        with tempfile.TemporaryDirectory() as directory:
            graph = _graph(
                directory,
                pool=["a/reasoning", "b/reasoning", "c/reasoning"])
            answers = iter([
                "def add(a, b):\n    return a + b\n",
                "DROP TABLE users;",
                "print('hello world')",
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
                    "implement add()", quorum=2, min_agreement=0.6)

            self.assertFalse(result.agreed)
            self.assertIn("no cluster meets quorum", decision.note)

    def test_four_models_two_clusters(self) -> None:
        """Four models: two clusters of two. Largest cluster wins."""
        with tempfile.TemporaryDirectory() as directory:
            graph = _graph(
                directory,
                pool=["a/reasoning", "b/reasoning",
                       "c/reasoning", "d/reasoning"])
            # Use more distinct responses to ensure separate clusters
            answers = iter([
                "def add(a, b):\n    return a + b\n",
                "def add(a, b):\n    return a + b\n",
                "DROP TABLE users; -- malicious",
                "DROP TABLE users; -- malicious",
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
                    "implement add()", quorum=2, min_agreement=0.6)

            self.assertTrue(result.agreed)
            self.assertEqual(len(result.clusters), 2)
            # Both clusters have size 2, first one wins
            self.assertEqual(len(result.clusters[0]), 2)
            self.assertEqual(len(result.clusters[1]), 2)


if __name__ == "__main__":
    unittest.main()
