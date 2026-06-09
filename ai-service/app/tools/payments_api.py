"""Tool: query the C_V_PAYMENT (CS_PAYMENT) table via the TCMP destination.

Filters supported in this iteration:
- PERIODSEQ      (integer)
- EARNINGGROUPID (string)
- EARNINGCODEID  (string)

At least ONE filter must be provided. The requirement is communicated to
the model via the tool description and the parameter docs, and enforced as
a runtime guard so the LLM can never trigger a wide-open call.
(Top-level `anyOf` cannot be used here because the AI Core function-calling
validator rejects oneOf/anyOf/allOf/enum/not at the schema root.)
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

from ..config import get_settings
from ..destination_client import destinations

log = logging.getLogger(__name__)


# OpenAI-style tool spec — registered with the LLM.
PAYMENTS_TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "query_payments",
        "description": (
            "Fetch payment records from the SAP Incentive Management "
            "CS_PAYMENT table. AT LEAST ONE of `periodseq`, `earninggroupid`, "
            "or `earningcodeid` MUST be provided. Do NOT call this tool "
            "without at least one of those filters — ask the user instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "periodseq": {
                    "type": "integer",
                    "description": (
                        "PERIODSEQ — the numeric sequence ID of the "
                        "compensation period (e.g. 2533274790396033)."
                    ),
                },
                "earninggroupid": {
                    "type": "string",
                    "description": (
                        "EARNINGGROUPID — the earning group label, "
                        "e.g. 'Commission', 'Bonus'."
                    ),
                },
                "earningcodeid": {
                    "type": "string",
                    "description": (
                        "EARNINGCODEID — the earning code label, "
                        "e.g. 'Named Account'."
                    ),
                },
                "top": {
                    "type": "integer",
                    "description": "Max rows to fetch (default 200, max 1000).",
                    "default": 200,
                    "minimum": 1,
                    "maximum": 1000,
                },
            },
            "additionalProperties": False,
        },
    },
}


def _odata_escape(value: str) -> str:
    """OData v4 string literals: escape single quotes by doubling them."""
    return value.replace("'", "''")


def _build_filter(
    periodseq: int | None,
    earninggroupid: str | None,
    earningcodeid: str | None,
) -> str:
    parts: list[str] = []
    if periodseq is not None:
        parts.append(f"PERIODSEQ eq {int(periodseq)}")
    if earninggroupid:
        parts.append(f"EARNINGGROUPID eq '{_odata_escape(earninggroupid)}'")
    if earningcodeid:
        parts.append(f"EARNINGCODEID eq '{_odata_escape(earningcodeid)}'")
    return " and ".join(parts)


# Columns we project to the LLM. Avoids the 60+ generic columns that would
# burn tokens without value to the user.
_KEY_COLUMNS = [
    "PAYMENTSEQ",
    "PAYEESEQ",
    "POSITIONSEQ",
    "PERIODSEQ",
    "EARNINGGROUPID",
    "EARNINGCODEID",
    "VALUE",
    "POSTPIPELINERUNDATE",
    "TRIALPIPELINERUNDATE",
]


async def query_payments(
    *,
    periodseq: int | None = None,
    earninggroupid: str | None = None,
    earningcodeid: str | None = None,
    top: int = 200,
) -> dict[str, Any]:
    """Execute the OData call and return a compact summary.

    The summary shape (intentionally small) is what we feed back into the LLM
    as the tool's observation.
    """
    # Runtime guard — defence in depth against the LLM violating the schema.
    if periodseq is None and not earninggroupid and not earningcodeid:
        return {
            "ok": False,
            "error": "missing_filter",
            "message": (
                "Refused: at least one of periodseq, earninggroupid, "
                "earningcodeid must be provided."
            ),
        }

    top = max(1, min(int(top or 200), 1000))

    settings = get_settings()
    dest = await destinations().get(settings.tcmp_destination)

    filter_str = _build_filter(periodseq, earninggroupid, earningcodeid)
    base = f"{dest.url}{settings.tcmp_base_path}".rstrip("/")
    url = (
        f"{base}/C_V_PAYMENT/C_V_PAYMENT"
        f"?$filter={quote(filter_str, safe='')}&$top={top}"
    )

    headers = {"Accept": "application/json", **dest.headers}

    log.info("query_payments → GET %s", url)
    log.info("query_payments → filter=%s", filter_str)
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url, headers=headers)

    content_type = resp.headers.get("content-type", "")
    log.info(
        "query_payments ← status=%s content-type=%s bytes=%d",
        resp.status_code,
        content_type,
        len(resp.content),
    )

    if resp.status_code >= 400:
        log.error(
            "TCMP call failed: status=%s body=%s",
            resp.status_code,
            resp.text[:500],
        )
        return {
            "ok": False,
            "error": "upstream_http_error",
            "status": resp.status_code,
            "message": resp.text[:500],
        }

    try:
        body = resp.json()
    except ValueError:
        snippet = resp.text[:500] if resp.text else "<empty body>"
        log.error(
            "TCMP returned non-JSON: content-type=%s body=%s",
            content_type,
            snippet,
        )
        return {
            "ok": False,
            "error": "upstream_non_json",
            "status": resp.status_code,
            "contentType": content_type,
            "message": (
                "Upstream returned a non-JSON response (often an HTML "
                "login page or an XML OData v2 payload). Body preview: "
                + snippet
            ),
        }
    records: list[dict[str, Any]] = body.get("value") or []

    # Server-side $count is not requested; expose what we actually pulled.
    total_value = 0.0
    by_group: dict[str, dict[str, float]] = {}
    by_code: dict[str, dict[str, float]] = {}

    for r in records:
        val = float(r.get("VALUE") or 0)
        total_value += val
        g = r.get("EARNINGGROUPID") or "(none)"
        c = r.get("EARNINGCODEID") or "(none)"
        gg = by_group.setdefault(g, {"count": 0, "amount": 0.0})
        gg["count"] += 1
        gg["amount"] += val
        cc = by_code.setdefault(c, {"count": 0, "amount": 0.0})
        cc["count"] += 1
        cc["amount"] += val

    # Slim each record to the key columns only.
    sample = [
        {k: r.get(k) for k in _KEY_COLUMNS}
        for r in records[:10]
    ]

    return {
        "ok": True,
        "filtersApplied": {
            "periodseq": periodseq,
            "earninggroupid": earninggroupid,
            "earningcodeid": earningcodeid,
        },
        "rowsReturned": len(records),
        "rowsCapAt": top,
        "truncated": len(records) >= top,
        "totalValue": round(total_value, 2),
        "byEarningGroup": {
            k: {"count": v["count"], "amount": round(v["amount"], 2)}
            for k, v in sorted(by_group.items(), key=lambda x: -x[1]["amount"])
        },
        "byEarningCode": {
            k: {"count": v["count"], "amount": round(v["amount"], 2)}
            for k, v in sorted(by_code.items(), key=lambda x: -x[1]["amount"])
        },
        "sample": sample,
    }
