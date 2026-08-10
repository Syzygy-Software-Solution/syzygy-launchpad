"""Super-agent (router).

For iteration 1 we only have one sub-agent (Payment Traceability), so the
router trivially dispatches to it. The shape is kept LLM-routable so adding
more agents in the future is a config-only change.

Routing strategy:
- If exactly one agent is registered → bypass the LLM, route directly.
- If two or more → ask the LLM to pick by name (very small prompt).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .agents.base import Agent, AgentRunResult
from .agents.payment_traceability import build_agent as build_payment_agent
from .aicore_client import chat_completion
from .config import get_settings
from .planner import run_planner_agent

log = logging.getLogger(__name__)


@dataclass
class OrchestratorResult:
    agent: str
    reply: str
    tool_calls: list[dict[str, Any]]
    datasets: list[dict[str, Any]] = field(default_factory=list)
    charts: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    tokens: int = 0


class Orchestrator:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        self.register(build_payment_agent())

    def register(self, agent: Agent) -> None:
        self._agents[agent.name] = agent

    @property
    def agents(self) -> dict[str, Agent]:
        return self._agents

    async def route(self, user_messages: list[dict[str, Any]]) -> str:
        """Return the name of the sub-agent that should handle this turn."""
        if len(self._agents) == 1:
            return next(iter(self._agents))

        catalog = "\n".join(
            f"- {name}: {a.description}" for name, a in self._agents.items()
        )
        router_messages = [
            {
                "role": "system",
                "content": (
                    "You are a router. Given a user message and a catalog of "
                    "agents, reply with EXACTLY the agent name (no quotes, no "
                    "punctuation) that should handle it. If none fit, reply "
                    "with the closest match.\n\n"
                    f"Agents:\n{catalog}"
                ),
            },
            *user_messages[-1:],  # last user turn is enough for routing
        ]
        response = await chat_completion(
            messages=router_messages, temperature=0.0, max_tokens=20
        )
        pick = (
            (response.get("choices") or [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
            .lower()
        )
        if pick in self._agents:
            return pick
        log.warning("Router returned unknown agent '%s' — falling back", pick)
        return next(iter(self._agents))

    async def handle(self, user_messages: list[dict[str, Any]]) -> OrchestratorResult:
        agent_name = await self.route(user_messages)
        agent = self._agents[agent_name]
        result: AgentRunResult = await run_planner_agent(agent, user_messages)
        return OrchestratorResult(
            agent=agent_name,
            reply=result.reply,
            tool_calls=result.tool_calls,
            datasets=result.datasets,
            charts=result.charts,
            model=get_settings().genai_chat_model,
            tokens=result.tokens,
        )


_orchestrator: Orchestrator | None = None


def orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
