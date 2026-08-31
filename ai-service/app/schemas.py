"""Pydantic request / response models for the chat API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list, min_length=1)


class ToolCallTrace(BaseModel):
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


class ChatResponse(BaseModel):
    reply: str
    agent: str
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    datasets: list[dict[str, Any]] = Field(default_factory=list)
    charts: list[dict[str, Any]] = Field(default_factory=list)
    model: str = ""
    tokens: int = 0
    steps: list[dict[str, Any]] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    version: str


# JavaScript has no integer type: `JSON.parse` turns every JSON number into a
# double, so any surrogate key past 2^53 is rounded the moment the browser
# reads it — the same off-by-one this service now avoids on the way IN. The
# rendered answer and `datasets` already carry ids as strings; this does the
# same for the raw tool trace, which is the only place raw ints still leave the
# service.
_MAX_EXACT_INT = 2 ** 53


def json_safe_ints(value: Any) -> Any:
    """Recursively render ints too large for a double as strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) > _MAX_EXACT_INT:
        return str(value)
    if isinstance(value, dict):
        return {k: json_safe_ints(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe_ints(v) for v in value]
    return value
