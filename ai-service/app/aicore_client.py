"""Thin AI Core (Generative AI Hub) chat-completions client."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import get_settings
from .destination_client import destinations

log = logging.getLogger(__name__)


class AiCoreError(RuntimeError):
    """Raised when AI Core returns a non-2xx response."""


async def chat_completion(
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] = "auto",
    temperature: float = 0.2,
    max_tokens: int = 1024,
) -> dict[str, Any]:
    """Call the configured gpt-4o-mini deployment with OpenAI-shaped payload.

    Returns the parsed JSON response (an OpenAI chat.completion object).
    """
    settings = get_settings()
    dest = await destinations().get(settings.genai_destination)

    url = (
        f"{dest.url}/v2/inference/deployments/"
        f"{settings.genai_chat_deployment_id}/chat/completions"
        f"?api-version={settings.ai_core_api_version}"
    )

    payload: dict[str, Any] = {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice

    headers = {"Content-Type": "application/json", **dest.headers}

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers=headers, json=payload)

    if resp.status_code >= 400:
        log.error(
            "AI Core call failed: status=%s body=%s", resp.status_code, resp.text[:500]
        )
        raise AiCoreError(
            f"AI Core HTTP {resp.status_code}: {resp.text[:300]}"
        )

    return resp.json()
