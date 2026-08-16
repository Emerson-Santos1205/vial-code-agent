from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Agent:
    name: str
    role: str
    run: Callable[[str], str]


class MultiAgentTeam:
    """Deterministic fan-out/fan-in orchestration for specialist agents.

    ``events`` is an optional :class:`vial_code_agent.events.EventStore` hub:
    each agent publishes a small ``AGENT_RUN`` event (resource = agent name)
    so other agents can react to ``ΔState`` without exchanging full context.
    """

    def __init__(self, agents: list[Agent], events=None,
                 actor: str = "vial-code-agent") -> None:
        if not agents:
            raise ValueError("at least one agent is required")
        self.agents = agents
        self.events = events
        self.actor = actor
        self._versions: dict[str, int] = {}

    def run(self, task: str) -> dict[str, str]:
        results: dict[str, str] = {}
        for agent in self.agents:
            outcome = agent.run(task)
            results[agent.name] = outcome
            if self.events is not None:
                version = self._versions.get(agent.name, 0) + 1
                self._versions[agent.name] = version
                try:
                    self.events.publish(
                        "AGENT_RUN", agent.name, version, self.actor,
                        data={"task": task, "outcome": outcome[:200]})
                except PermissionError:
                    pass
        return results

    def synthesize(self, task: str, synthesizer: Callable[[str, dict[str, str]], str]) -> str:
        return synthesizer(task, self.run(task))
