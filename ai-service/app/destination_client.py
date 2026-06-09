"""SAP BTP Destination service client.

Resolves a named destination (e.g. `aicore-genai`, `TCMP_DEST`) into:
- A target URL.
- An OAuth2 bearer token (when the destination is configured with
  OAuth2ClientCredentials).
- Any additional headers configured on the destination (e.g. the
  `AI-Resource-Group` header we added on `aicore-genai`).

Credentials for the destination service itself are read from CF's
`VCAP_SERVICES.destination[0].credentials`.

A simple in-memory token cache (per destination name) is used. Tokens are
refreshed ~60s before their advertised expiry.

For local dev (no VCAP_SERVICES) we fall back to a raw AI Core service key
JSON from `AICORE_SERVICE_KEY` — only the `aicore-genai` destination is
resolvable in this mode.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import get_settings

log = logging.getLogger(__name__)


@dataclass
class ResolvedDestination:
    """A destination resolved to a usable URL + auth headers."""
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class _CachedToken:
    token: str
    expires_at: float  # epoch seconds


class DestinationClient:
    """Async client for SAP BTP Destination service.

    Single instance per process. Caches the destination-service OAuth token
    plus the per-destination OAuth tokens that the service returns.
    """

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=20.0)
        self._lock = asyncio.Lock()
        self._svc_creds: dict[str, Any] | None = None
        self._svc_token: _CachedToken | None = None
        self._dest_cache: dict[str, tuple[ResolvedDestination, float]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ public
    async def get(self, name: str) -> ResolvedDestination:
        """Return a resolved destination, refreshing if cached value expired."""
        # 5-minute destination-config cache; auth token inside is itself
        # short-lived but is provided by the destination service per call.
        async with self._lock:
            cached = self._dest_cache.get(name)
            if cached and cached[1] > time.time():
                return cached[0]

            resolved = await self._resolve(name)
            self._dest_cache[name] = (resolved, time.time() + 300)
            return resolved

    # --------------------------------------------------------------- internals
    def _load_svc_creds(self) -> dict[str, Any]:
        if self._svc_creds is not None:
            return self._svc_creds

        settings = get_settings()
        vcap = settings.vcap_services
        if not vcap:
            raise RuntimeError(
                "VCAP_SERVICES is empty. For local dev set AICORE_SERVICE_KEY "
                "and use the dev-fallback path (only aicore-genai works locally)."
            )
        try:
            services = json.loads(vcap)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"VCAP_SERVICES is not valid JSON: {e}") from e

        dests = services.get("destination") or []
        if not dests:
            raise RuntimeError("No `destination` service bound to this app.")
        self._svc_creds = dests[0]["credentials"]
        return self._svc_creds

    async def _get_svc_token(self) -> str:
        if self._svc_token and self._svc_token.expires_at > time.time() + 60:
            return self._svc_token.token

        creds = self._load_svc_creds()
        token_url = f"{creds['url']}/oauth/token"
        resp = await self._http.post(
            token_url,
            data={"grant_type": "client_credentials"},
            auth=(creds["clientid"], creds["clientsecret"]),
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        self._svc_token = _CachedToken(
            token=data["access_token"],
            expires_at=time.time() + int(data.get("expires_in", 3600)),
        )
        return self._svc_token.token

    async def _resolve(self, name: str) -> ResolvedDestination:
        # Local-dev fallback: synthesise the aicore-genai destination from a
        # raw service key. TCMP_DEST cannot be resolved this way.
        settings = get_settings()
        if not settings.vcap_services and name == settings.genai_destination:
            return await self._resolve_aicore_from_service_key()

        svc_token = await self._get_svc_token()
        creds = self._load_svc_creds()
        url = f"{creds['uri']}/destination-configuration/v1/destinations/{name}"
        resp = await self._http.get(
            url,
            headers={"Authorization": f"Bearer {svc_token}"},
        )
        if resp.status_code == 404:
            raise RuntimeError(f"Destination '{name}' not found in this subaccount.")
        resp.raise_for_status()
        payload = resp.json()

        cfg = payload.get("destinationConfiguration", {})
        target_url = cfg.get("URL", "").rstrip("/")
        if not target_url:
            raise RuntimeError(f"Destination '{name}' has no URL.")

        headers: dict[str, str] = {}

        # OAuth token (when destination has OAuth2ClientCredentials etc.)
        auth_tokens = payload.get("authTokens") or []
        if auth_tokens:
            t = auth_tokens[0]
            if t.get("error"):
                raise RuntimeError(
                    f"Destination '{name}' auth error: {t['error']}"
                )
            headers["Authorization"] = f"{t.get('type', 'Bearer')} {t['value']}"

        # Surface "URL.headers.<X>" properties as outgoing request headers —
        # this is how `AI-Resource-Group: default` reaches AI Core.
        for key, value in cfg.items():
            if key.startswith("URL.headers."):
                headers[key[len("URL.headers."):]] = value

        return ResolvedDestination(name=name, url=target_url, headers=headers)

    async def _resolve_aicore_from_service_key(self) -> ResolvedDestination:
        settings = get_settings()
        try:
            sk = json.loads(settings.aicore_service_key_json)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                "AICORE_SERVICE_KEY env var is required for local dev fallback."
            ) from e

        token_url = f"{sk['url']}/oauth/token"
        resp = await self._http.post(
            token_url,
            data={"grant_type": "client_credentials"},
            auth=(sk["clientid"], sk["clientsecret"]),
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]

        return ResolvedDestination(
            name=settings.genai_destination,
            url=sk["serviceurls"]["AI_API_URL"].rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "AI-Resource-Group": settings.ai_resource_group,
            },
        )


# Singleton — created lazily, closed on shutdown.
_dest_client: DestinationClient | None = None


def destinations() -> DestinationClient:
    global _dest_client
    if _dest_client is None:
        _dest_client = DestinationClient()
    return _dest_client


async def shutdown_destinations() -> None:
    global _dest_client
    if _dest_client is not None:
        await _dest_client.aclose()
        _dest_client = None
