from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Agent:
    name: str
    role: str
    run: Callable[[str], str]


class MultiAgentTeam:
    """Deterministic fan-out/fan-in orchestration for specialist agents."""

    def __init__(self, agents: list[Agent]) -> None:
        if not agents:
            raise ValueError("at least one agent is required")
        self.agents = agents

    def run(self, task: str) -> dict[str, str]:
        return {agent.name: agent.run(task) for agent in self.agents}

    def synthesize(self, task: str, synthesizer: Callable[[str, dict[str, str]], str]) -> str:
        return synthesizer(task, self.run(task))
