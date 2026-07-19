"""Generic entity-query tool.

Replaces the single-purpose `query_payments` tool. Drives every entity
defined in `ai-service/catalog/*.yaml` through one OData call path.

Tool contract exposed to the LLM:
    query_entity(entity: str, filters: object, top: int = 200)

The handler:
1. Validates `entity` against the catalog.
2. Validates each filter against the entity spec (key, type, requireOneOf).
3. Builds an OData v4 $filter string using the column/op metadata.
4. Calls the Datasphere consumption layer via the TCMP destination.
5. Returns a compact summary: filtersApplied, rowsReturned, sample, and
   any aggregations declared in the entity card.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

from ..catalog import EntitySpec, FilterSpec, catalog
from ..config import get_settings
from ..destination_client import destinations

log = logging.getLogger(__name__)


# OpenAI-style tool spec. Note: AI Core rejects top-level anyOf/oneOf/
# allOf/enum/not in the parameters schema, so we use a flat object and
# enforce semantic validation at runtime instead.
QUERY_ENTITY_TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "query_entity",
        "description": (
            "Query a single entity from the SAP Datasphere consumption "
            "layer. The system prompt lists every available entity and "
            "the filter keys it accepts. At least one filter (from the "
            "entity's requireOneOf set) MUST be supplied."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": (
                        "Entity name. Must match one of the entities "
                        "listed in the system prompt (e.g. 'cs_payment', "
                        "'cs_period', 'cs_periodtype')."
                    ),
                },
                "filters": {
                    "type": "object",
                    "description": (
                        "Filter map. Keys are the filter names for the "
                        "chosen entity (see system prompt). Values follow "
                        "the documented type (integer, string, or ISO "
                        "8601 UTC datetime with trailing Z)."
                    ),
                    "additionalProperties": True,
                },
                "top": {
                    "type": "integer",
                    "description": "Max rows to fetch (default 200, max 1000).",
                    "default": 200,
                    "minimum": 1,
                    "maximum": 1000,
                },
                "orderby": {
                    "type": "string",
                    "description": (
                        "Optional OData $orderby clause. Use the "
                        "PROJECTED column name (e.g. 'VALUE desc', "
                        "'STARTDATE asc'). REQUIRED when the user "
                        "asks for 'top N by X' or 'highest/lowest X' "
                        "— pair it with top=N so the upstream returns "
                        "the correct rows in the correct order."
                    ),
                },
            },
            "required": ["entity", "filters"],
            "additionalProperties": False,
        },
    },
}


# ---------------------------------------------------------------------------
# Filter coercion + OData $filter assembly
# ---------------------------------------------------------------------------

def _odata_escape(value: str) -> str:
    return value.replace("'", "''")


def _format_value(spec: FilterSpec, raw: Any) -> str:
    """Render a python value as an OData v4 literal of the right type."""
    if spec.type == "integer":
        return str(int(raw))
    if spec.type == "datetime":
        # OData v4 Edm.DateTimeOffset: literal must NOT be quoted.
        # Trust the LLM-provided ISO 8601 Z string; reject anything else.
        text = str(raw).strip()
        if not text.endswith("Z"):
            raise ValueError(
                f"datetime filter '{spec.key}' must be ISO 8601 with "
                f"trailing 'Z' (got: {text!r})"
            )
        return text
    # default: string
    return f"'{_odata_escape(str(raw))}'"


def _format_in_clause(spec: FilterSpec, raw: Any) -> str:
    """Render a list value as a parenthesised chain of `eq ... or ...`.

    The OData v4 `in` operator is not supported by the SAP Datasphere
    consumption layer (returns HTTP 400 "Edm.Int64 is not compatible
    to Edm.Boolean"). The equivalent that works on every OData v4
    server is `(COL eq v1 or COL eq v2 or ...)`.
    """
    if not isinstance(raw, (list, tuple)):
        raise ValueError(
            f"filter '{spec.key}' uses op=in and requires a list value "
            f"(got: {type(raw).__name__})"
        )
    if not raw:
        raise ValueError(f"filter '{spec.key}' (op=in) must not be empty")
    # Dedup while preserving order so the OR chain is minimal.
    seen: list[Any] = []
    for v in raw:
        if v not in seen:
            seen.append(v)
    pieces = [f"{spec.column} eq {_format_value(spec, v)}" for v in seen]
    return "(" + " or ".join(pieces) + ")"


def _build_filter(entity: EntitySpec, filters: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, raw in filters.items():
        if raw is None or raw == "":
            continue
        spec = entity.filters.get(key)
        if spec is None:
            raise ValueError(
                f"unknown filter '{key}' for entity '{entity.name}'. "
                f"Allowed: {sorted(entity.filters)}"
            )
        if spec.op == "in":
            # _format_in_clause already wraps in parens, so it composes
            # cleanly with surrounding `and` clauses.
            parts.append(_format_in_clause(spec, raw))
        else:
            parts.append(
                f"{spec.column} {spec.op} {_format_value(spec, raw)}"
            )
    return " and ".join(parts)


def _aggregate(
    entity: EntitySpec, records: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compute the aggregations declared in the entity card."""
    out: dict[str, Any] = {}
    for agg_name, agg_spec in entity.aggregations.items():
        kind = agg_spec.get("type")
        if kind == "sum":
            col = agg_spec["column"]
            total = sum(float(r.get(col) or 0) for r in records)
            out[agg_name] = round(total, 2)
        elif kind == "groupBy":
            by = agg_spec["by"]
            metric = agg_spec["metric"]
            buckets: dict[str, dict[str, float]] = {}
            for r in records:
                key = r.get(by) or "(none)"
                val = float(r.get(metric) or 0)
                b = buckets.setdefault(key, {"count": 0, "amount": 0.0})
                b["count"] += 1
                b["amount"] += val
            out[agg_name] = {
                k: {"count": v["count"], "amount": round(v["amount"], 2)}
                for k, v in sorted(
                    buckets.items(), key=lambda x: -x[1]["amount"]
                )
            }
    return out


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

async def query_entity(
    *,
    entity: str,
    filters: dict[str, Any] | None = None,
    top: int = 200,
    orderby: str | None = None,
    extract_columns: list[str] | None = None,
) -> dict[str, Any]:
    """Query one catalog entity via the Datasphere consumption layer.

    `extract_columns` is an internal (non-LLM) hook used by the planner's
    executor: when supplied, the result includes an `extracts` map of DISTINCT
    values for each requested column computed over ALL returned records (not the
    capped `sample`), so multi-hop feed-forward stays correct even when the
    result set is larger than the sample window.
    """
    registry = catalog()
    spec = registry.get(entity)
    if spec is None:
        return {
            "ok": False,
            "error": "unknown_entity",
            "message": (
                f"Unknown entity '{entity}'. Allowed: {sorted(registry)}."
            ),
        }

    filters = filters or {}

    # Runtime guard: requireOneOf
    if spec.require_one_of:
        present = [
            k for k in spec.require_one_of
            if filters.get(k) not in (None, "")
        ]
        if not present:
            return {
                "ok": False,
                "error": "missing_filter",
                "message": (
                    f"Entity '{entity}' requires at least one of: "
                    + ", ".join(spec.require_one_of)
                ),
            }

    top = max(1, min(int(top or 200), 1000))

    try:
        filter_str = _build_filter(spec, filters)
    except ValueError as e:
        return {"ok": False, "error": "bad_filter", "message": str(e)}

    settings = get_settings()
    dest = await destinations().get(settings.tcmp_destination)
    base = f"{dest.url}{settings.tcmp_base_path}".rstrip("/")
    url = (
        f"{base}{spec.endpoint}"
        f"?$filter={quote(filter_str, safe='')}&$top={top}"
    )
    if orderby:
        # Trust the LLM-supplied clause (column names come from the
        # catalog/projection it can see). quote() leaves spaces as
        # %20 which OData accepts.
        url += f"&$orderby={quote(orderby, safe=',')}"
        log.info("query_entity[%s] → orderby=%s", entity, orderby)

    headers = {"Accept": "application/json", **dest.headers}
    log.info("query_entity[%s] → GET %s", entity, url)
    log.info("query_entity[%s] → filter=%s", entity, filter_str)

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.get(url, headers=headers)

    content_type = resp.headers.get("content-type", "")
    log.info(
        "query_entity[%s] ← status=%s content-type=%s bytes=%d",
        entity, resp.status_code, content_type, len(resp.content),
    )

    if resp.status_code >= 400:
        log.error(
            "TCMP call failed entity=%s status=%s body=%s",
            entity, resp.status_code, resp.text[:500],
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
            "TCMP returned non-JSON entity=%s content-type=%s body=%s",
            entity, content_type, snippet,
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

    # Optional per-entity dedup. Time-versioned tables like CS_PARTICIPANT
    # return many rows per logical key (one per effective-dated version).
    # Collapsing to the first occurrence keeps the LLM context small and
    # the answer stable.
    if spec.dedupe_by and records:
        seen: set[Any] = set()
        deduped: list[dict[str, Any]] = []
        for r in records:
            k = r.get(spec.dedupe_by)
            if k in seen:
                continue
            seen.add(k)
            deduped.append(r)
        records = deduped

    projection = spec.projection or list(records[0].keys()) if records else []
    # Cap at 50 (was 10). The LLM does client-side reasoning over this
    # slice, so a bigger window lets it answer "top N by X" honestly
    # when paired with $orderby. 50 keeps prompt tokens bounded.
    sample = [{k: r.get(k) for k in projection} for r in records[:50]]
    aggregations = _aggregate(spec, records)

    result: dict[str, Any] = {
        "ok": True,
        "entity": entity,
        "filtersApplied": {k: v for k, v in filters.items() if v not in (None, "")},
        "rowsReturned": len(records),
        "rowsCapAt": top,
        "truncated": len(records) >= top,
        "sample": sample,
    }
    if aggregations:
        result.update(aggregations)

    # Internal feed-forward hook: distinct values over ALL records (order
    # preserved), used by the planner executor to chain multi-hop steps.
    if extract_columns:
        extracts: dict[str, list[Any]] = {}
        for col in extract_columns:
            seen_vals: list[Any] = []
            seen_set: set[Any] = set()
            for r in records:
                v = r.get(col)
                if v in (None, "") or v in seen_set:
                    continue
                seen_set.add(v)
                seen_vals.append(v)
            extracts[col] = seen_vals
        result["extracts"] = extracts

    return result
