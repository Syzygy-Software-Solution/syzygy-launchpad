"""Agent base abstractions + run loop.

A sub-agent here is a small dataclass with:
- a system prompt
- a list of OpenAI-shaped tool specs
- a mapping from tool name -> async python callable

The shared `run_agent` helper performs the chat <-> tool loop, capping
iterations to avoid runaway loops.

This is deliberately not MCP — same shape, no transport. Wrapping these tools
in an MCP server later is a thin adapter, not a rewrite.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ..aicore_client import chat_completion

log = logging.getLogger(__name__)


ToolHandler = Callable[..., Awaitable[dict[str, Any]]]


@dataclass
class Agent:
    name: str
    description: str
    system_prompt: str
    tool_specs: list[dict[str, Any]] = field(default_factory=list)
    tool_handlers: dict[str, ToolHandler] = field(default_factory=dict)
    temperature: float = 0.1
    max_tool_iterations: int = 4


@dataclass
class AgentRunResult:
    reply: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw_messages: list[dict[str, Any]] = field(default_factory=list)


async def run_agent(
    agent: Agent,
    user_messages: list[dict[str, Any]],
) -> AgentRunResult:
    """Drive the LLM tool-calling loop for a single agent.

    `user_messages` is the running chat history from the browser
    ({role, content}). The agent's system prompt is prepended.
    """
    # Date math (e.g. "last 2 months") only works if the LLM knows what
    # today is. We inject a tiny dynamic system message after the agent's
    # static prompt so every turn sees the current UTC date.
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": agent.system_prompt},
        {"role": "system", "content": f"Today's date (UTC) is {today_utc}."},
        *user_messages,
    ]
    tool_calls_executed: list[dict[str, Any]] = []

    for iteration in range(agent.max_tool_iterations + 1):
        response = await chat_completion(
            messages=messages,
            tools=agent.tool_specs or None,
            temperature=agent.temperature,
            max_tokens=1024,
        )

        choice = (response.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        finish_reason = choice.get("finish_reason")

        # Always append the assistant turn (with tool_calls if any) so the
        # next iteration's `tool` messages reference a valid call id.
        messages.append(
            {
                "role": "assistant",
                "content": msg.get("content"),
                "tool_calls": msg.get("tool_calls"),
            }
        )

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls or finish_reason == "stop":
            return AgentRunResult(
                reply=(msg.get("content") or "").strip(),
                tool_calls=tool_calls_executed,
                raw_messages=messages,
            )

        if iteration == agent.max_tool_iterations:
            log.warning(
                "Agent %s hit max tool iterations (%d)",
                agent.name,
                agent.max_tool_iterations,
            )
            return AgentRunResult(
                reply=(
                    "I attempted multiple tool calls but could not finalise "
                    "an answer. Please rephrase or narrow your request."
                ),
                tool_calls=tool_calls_executed,
                raw_messages=messages,
            )

        # Execute every tool call requested in this turn.
        for call in tool_calls:
            tool_name = (call.get("function") or {}).get("name") or ""
            raw_args = (call.get("function") or {}).get("arguments") or "{}"
            try:
                kwargs = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                kwargs = {}

            handler = agent.tool_handlers.get(tool_name)
            if handler is None:
                result: dict[str, Any] = {
                    "ok": False,
                    "error": "unknown_tool",
                    "message": f"No handler registered for tool '{tool_name}'.",
                }
            else:
                try:
                    result = await handler(**kwargs)
                except TypeError as e:
                    result = {
                        "ok": False,
                        "error": "bad_arguments",
                        "message": str(e),
                    }
                except Exception as e:  # noqa: BLE001 - surface to LLM
                    log.exception("Tool %s failed", tool_name)
                    result = {
                        "ok": False,
                        "error": "tool_exception",
                        "message": str(e),
                    }

            tool_calls_executed.append(
                {"name": tool_name, "arguments": kwargs, "result": result}
            )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": tool_name,
                    "content": json.dumps(result, default=str),
                }
            )

    # Should be unreachable.
    return AgentRunResult(
        reply="(no response)",
        tool_calls=tool_calls_executed,
        raw_messages=messages,
    )
