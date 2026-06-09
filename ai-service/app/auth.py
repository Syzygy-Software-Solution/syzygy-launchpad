"""Lightweight JWT inspection.

For v1 the python service runs strictly behind the launchpad approuter,
which already enforces XSUAA auth before forwarding the request. We
therefore do *unverified* JWT decoding here, purely to extract user-id /
email for logging. Full signature validation will be added in a later
iteration (sap-xssec or PyJWT + JWKS).

If `DEV_SKIP_AUTH=true` (local dev), the Authorization header is optional
and we synthesise an anonymous principal.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import jwt
from fastapi import Header, HTTPException, status

from .config import get_settings

log = logging.getLogger(__name__)


@dataclass
class Principal:
    user_id: str
    email: str | None = None
    name: str | None = None


def _decode_unverified(token: str) -> dict:
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        ) from e


async def current_principal(
    authorization: str | None = Header(default=None),
) -> Principal:
    settings = get_settings()

    if not authorization:
        if settings.dev_skip_auth:
            return Principal(user_id="dev-user", email="dev@local")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    if not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must use Bearer scheme",
        )

    token = authorization.split(" ", 1)[1].strip()
    claims = _decode_unverified(token)

    return Principal(
        user_id=str(claims.get("user_uuid") or claims.get("user_name") or claims.get("sub") or "unknown"),
        email=claims.get("email"),
        name=claims.get("user_name") or claims.get("given_name"),
    )
