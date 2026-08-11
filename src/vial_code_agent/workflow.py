from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(frozen=True)
class StepResult:
    name: str
    output: str
    passed: bool


class SequentialWorkflow:
    """Run named tasks in order, stopping at the first failure."""

    def __init__(self, runner: Callable[[str, str], str]) -> None:
        self.runner = runner

    def run(self, steps: Iterable[tuple[str, str]], initial_context: str = "") -> list[StepResult]:
        context = initial_context
        results: list[StepResult] = []
        for name, task in steps:
            try:
                output = self.runner(task, context)
            except Exception as error:  # keep the failed step visible to callers
                results.append(StepResult(name, str(error), False))
                break
            results.append(StepResult(name, output, True))
            context = output
        return results
