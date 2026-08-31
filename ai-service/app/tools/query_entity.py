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

import asyncio
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
# Fetch: $select, request batching, and paging
# ---------------------------------------------------------------------------
# Three independent limits, deliberately NOT conflated:
#   _MAX_URL_CHARS - transport limit. Datasphere rejects URLs past roughly 8 KB
#                    (measured: 200 bigint values in an OR chain = 9,520 chars).
#                    Oversized `in` lists are split across requests.
#   _PAGE_SIZE     - rows per HTTP request. Paging walks toward the caller's
#                    `top` so a card may ask for more than one page.
#   `top`          - how many rows the caller wants. Costs payload and latency,
#                    NOT tokens: rows reach the LLM only via the 50-row `sample`.
_MAX_URL_CHARS = 6000
_PAGE_SIZE = 1000
# Concurrent in-flight requests when a filter is split into batches.
_MAX_CONCURRENCY = 5

# ---------------------------------------------------------------------------
# Int64 precision
# ---------------------------------------------------------------------------
# Every surrogate key in this source (PAYMENTSEQ, DEPOSITSEQ, INCENTIVESEQ,
# CREDITSEQ, MEASUREMENTSEQ, ...) is a bigint, and many of them run past 2^53 —
# the largest integer an IEEE-754 double holds exactly. A key that crosses ANY
# hop parsing JSON numbers as doubles is silently rounded to the nearest
# representable value: 26177172834150613 comes back as ...612. Ids below 2^53
# (payee, period, position in most tenants) survive untouched, which is exactly
# why this shows up as "only some ids are off by one".
#
# OData v4 has a format parameter for precisely this problem:
# `IEEE754Compatible=true` tells the server to serialise Edm.Int64 and
# Edm.Decimal as JSON STRINGS, which no double ever touches. We ask for it on
# every request and convert the strings back to int/float here — driven by the
# entity card's declared column types — so every consumer downstream sees the
# same Python types it always has, only exact.
_ACCEPT_PLAIN = "application/json"
_ACCEPT_IEEE754 = "application/json;IEEE754Compatible=true"
_MAX_EXACT_INT = 2 ** 53
_INT_TYPES = {"bigint", "int", "integer"}

# Set to False for the life of the process if the source rejects the
# parameterised media type (HTTP 406), so we negotiate once, not per request.
_ieee754_accept = True
# (entity, column) pairs already reported — one log line per column, not per row.
_precision_warned: set[tuple[str, str]] = set()


def _accept_header() -> str:
    return _ACCEPT_IEEE754 if _ieee754_accept else _ACCEPT_PLAIN


def _warn_precision(entity: str, column: str) -> None:
    if (entity, column) in _precision_warned:
        return
    _precision_warned.add((entity, column))
    log.warning(
        "query_entity[%s] ← %s arrived as a JSON number larger than 2^53 "
        "despite Accept=%s. The source did not honour IEEE754Compatible, so "
        "this key may already be rounded (off by 1-2) before it reached us — "
        "that loss is not recoverable client-side and must be fixed at the "
        "source (expose the column as Edm.Int64 / a string, not a double).",
        entity, column, _ACCEPT_IEEE754,
    )


def _coerce_numeric(spec: EntitySpec, records: list[dict[str, Any]]) -> None:
    """Undo IEEE754Compatible string encoding, in place, per column type.

    Integer columns become `int` (parsed from text, so exact at any magnitude)
    and decimal columns become `float`, which is what every caller already
    expects. This is also the only place that can DETECT a rounding that
    happened upstream — an integer key still arriving as a JSON number past
    2^53 — so it says so in the log instead of rendering a wrong id silently.
    """
    for rec in records:
        for name, value in rec.items():
            col = spec.columns.get(name)
            if col is None or value is None:
                continue
            ctype = (col.type or "").lower()
            if ctype in _INT_TYPES:
                if isinstance(value, str):
                    try:
                        rec[name] = int(value)
                    except ValueError:
                        pass  # not a number after all — leave it alone
                elif isinstance(value, float):
                    # Already a double upstream. Keep it rendering as digits
                    # rather than as 2.6e+16, and flag the lost precision.
                    rec[name] = int(value)
                    _warn_precision(spec.name, name)
                elif isinstance(value, int) and abs(value) > _MAX_EXACT_INT:
                    _warn_precision(spec.name, name)
            elif ctype == "decimal" and isinstance(value, str):
                try:
                    rec[name] = float(value)
                except ValueError:
                    pass


def _select_columns(spec: EntitySpec) -> list[str]:
    """Columns the request must ask for: projection + anything code reads later.

    Without `$select` every column crosses the wire and is then thrown away by
    the projection step. Narrowing it is a large payload win on wide tables, but
    only if we keep the columns that dedupe, enrichment and joins depend on —
    hence the union rather than the projection alone.
    """
    if not spec.projection:
        return []  # no projection declared -> fetch everything, as before
    cols = list(spec.projection)
    seen = set(cols)
    extra: list[str] = []
    if spec.dedupe_by:
        extra.append(spec.dedupe_by)
    for rule in spec.enrichments or []:
        if rule.get("column"):
            extra.append(rule["column"])
    extra.extend(j.via for j in spec.joins)
    for c in extra:
        if c and c in spec.columns and c not in seen:
            seen.add(c)
            cols.append(c)
    return cols


def _split_in_filters(
    spec: EntitySpec, filters: dict[str, Any], budget: int
) -> list[dict[str, Any]]:
    """Split `filters` into request-sized batches.

    Returns `[filters]` unchanged whenever the rendered filter already fits, so
    every query that works today takes the exact same single-request path.

    Only the LARGEST `in` filter is split: chunking two of them at once would
    produce a cross-product of requests rather than a partition of the result
    set, which would both duplicate rows and change the meaning of the query.
    """
    # Measure the ENCODED length: quote() turns each space into %20 and each
    # paren into %28/%29, so a raw filter string understates the real URL cost
    # by roughly a third.
    def enc_len(f: dict[str, Any]) -> int:
        return len(quote(_build_filter(spec, f), safe=""))

    try:
        if enc_len(filters) <= budget:
            return [filters]
    except ValueError:
        return [filters]  # let the caller surface the real validation error

    candidates = [
        (k, list(v))
        for k, v in filters.items()
        if isinstance(v, (list, tuple))
        and len(v) > 1
        and (spec.filters.get(k).op if spec.filters.get(k) else None) == "in"
    ]
    if not candidates:
        return [filters]  # too long but nothing splittable; upstream will error

    key, values = max(candidates, key=lambda kv: len(kv[1]))
    values = list(dict.fromkeys(values))  # dedupe, preserve order
    others = {k: v for k, v in filters.items() if k != key}

    try:
        base = enc_len(others) if others else 0
        per = max(enc_len({key: values[:1]}), 1)
    except ValueError:
        return [filters]

    size = max(1, (budget - base - 32) // per)
    # `per` measures the first value, which carries the enclosing parens, so the
    # estimate runs slightly optimistic. Shrink proportionally to the overshoot
    # (with a little headroom) rather than halving, which would waste roughly a
    # third of each request's capacity and double the round trips.
    for _ in range(8):
        if size <= 1:
            break
        probe = dict(others)
        probe[key] = values[:size]
        try:
            actual = enc_len(probe)
        except ValueError:
            break
        if actual <= budget:
            break
        size = max(1, int(size * (budget / actual) * 0.95))

    batches: list[dict[str, Any]] = []
    for i in range(0, len(values), size):
        b = dict(others)
        b[key] = values[i : i + size]
        batches.append(b)
    log.info(
        "query_entity[%s] → splitting '%s' (%d values) into %d requests",
        spec.name, key, len(values), len(batches),
    )
    return batches


def _sort_records(records: list[dict[str, Any]], orderby: str) -> list[dict[str, Any]]:
    """Re-apply $orderby after merging batches.

    Each request sorts only its own slice, so a merged result is unordered.
    Nulls are kept last in both directions.
    """
    parts = orderby.replace(",", " ").split()
    if not parts:
        return records
    col = parts[0]
    desc = len(parts) > 1 and parts[1].lower() == "desc"
    non_null = [r for r in records if r.get(col) is not None]
    nulls = [r for r in records if r.get(col) is None]
    try:
        non_null.sort(key=lambda r: r.get(col), reverse=desc)
    except TypeError:
        return records  # mixed types — leave upstream order alone
    return non_null + nulls


def _build_url(
    base: str,
    spec: EntitySpec,
    filter_str: str,
    top: int,
    orderby: str | None,
    select: list[str],
    skip: int,
) -> str:
    url = (
        f"{base}{spec.endpoint}"
        f"?$filter={quote(filter_str, safe='')}&$top={top}"
    )
    if select:
        url += f"&$select={quote(','.join(select), safe=',')}"
    if orderby:
        # Trust the LLM-supplied clause (column names come from the
        # catalog/projection it can see). quote() leaves spaces as
        # %20 which OData accepts.
        url += f"&$orderby={quote(orderby, safe=',')}"
    if skip:
        url += f"&$skip={skip}"
    return url


async def _fetch_one(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    spec: EntitySpec,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """One GET. Returns (records, error_result); exactly one is non-None."""
    global _ieee754_accept

    entity = spec.name
    log.info("query_entity[%s] → GET %s", entity, url)
    resp = await client.get(url, headers={**headers, "Accept": _accept_header()})
    if resp.status_code == 406 and _ieee754_accept:
        # A few OData stacks reject the parameterised media type outright
        # rather than ignoring the parameter. Fall back once, for the process.
        log.warning(
            "query_entity[%s] ← 406 for Accept=%s; retrying as plain JSON. "
            "Int64 keys past 2^53 are then only as exact as the source makes "
            "them.",
            entity, _ACCEPT_IEEE754,
        )
        _ieee754_accept = False
        resp = await client.get(url, headers={**headers, "Accept": _ACCEPT_PLAIN})
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
        return None, {
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
        return None, {
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

    records = body.get("value") or []
    # Before ANY other code sees these rows: strings back to numbers, and a
    # loud line in the log if an id still came through a double.
    _coerce_numeric(spec, records)
    return records, None


async def _fetch_batch(
    client: httpx.AsyncClient,
    spec: EntitySpec,
    base: str,
    headers: dict[str, str],
    filter_str: str,
    top: int,
    orderby: str | None,
    select: list[str],
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None, bool]:
    """Fetch up to `top` rows for one filter string, paging as needed.

    Returns (records, error, hit_cap). `hit_cap` is True when the source still
    had rows at `top` — the signal the planner needs to know a chained step was
    truncated rather than complete.
    """
    records: list[dict[str, Any]] = []
    skip = 0
    while len(records) < top:
        want = min(_PAGE_SIZE, top - len(records))
        # Ask for ONE MORE row than we need. Without that probe, a result that
        # exactly fills the cap is indistinguishable from one that was cut
        # short, and every exactly-full result reports itself as truncated.
        url = _build_url(base, spec, filter_str, want + 1, orderby, select, skip)
        page, err = await _fetch_one(client, url, headers, spec)
        if err is not None:
            return None, err, False
        if len(page) <= want:
            records.extend(page)
            return records, None, False  # source exhausted
        records.extend(page[:want])      # the probe row proves more remain
        skip += want
    return records, None, True


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
    group_by: str | None = None,
    aggregate: str | None = None,
    aggregate_column: str | None = None,
) -> dict[str, Any]:
    """Query one catalog entity via the Datasphere consumption layer.

    `extract_columns` is an internal (non-LLM) hook used by the planner's
    executor: when supplied, the result includes an `extracts` map of DISTINCT
    values for each requested column computed over ALL returned records (not the
    capped `sample`), so multi-hop feed-forward stays correct even when the
    result set is larger than the sample window.

    `group_by` + `aggregate` ('count' | 'sum' with `aggregate_column`) computes a
    ranked group-by over ALL fetched records IN CODE (never pushed to OData),
    answering "which X has the most/highest Y" reliably.
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

    # Row cap comes from the entity card, not a hardcoded constant: a trace
    # table that must feed a later step needs a far higher ceiling than a
    # listing the user will read.
    top = max(1, min(int(top or spec.default_top), spec.max_top))

    settings = get_settings()
    dest = await destinations().get(settings.tcmp_destination)
    base = f"{dest.url}{settings.tcmp_base_path}".rstrip("/")
    # Accept is negotiated per request in _fetch_one (IEEE754Compatible,
    # with a one-time fallback), so it is deliberately not set here.
    headers = dict(dest.headers)
    select = _select_columns(spec)

    # Budget for the encoded $filter: the URL ceiling less the fixed prefix and
    # the other query options.
    fixed = (
        len(base) + len(spec.endpoint) + len(",".join(select))
        + len(orderby or "") + 96
    )
    batches = _split_in_filters(spec, filters, max(_MAX_URL_CHARS - fixed, 512))

    try:
        filter_strs = [_build_filter(spec, b) for b in batches]
    except ValueError as e:
        return {"ok": False, "error": "bad_filter", "message": str(e)}

    log.info("query_entity[%s] → filter=%s", entity, filter_strs[0])
    if orderby:
        log.info("query_entity[%s] → orderby=%s", entity, orderby)

    records: list[dict[str, Any]] = []
    hit_cap = False
    async with httpx.AsyncClient(timeout=120.0) as client:
        if len(filter_strs) == 1:
            # Single-request path — byte-identical in behaviour to before
            # batching existed, so nothing that works today can change.
            recs, err, hit_cap = await _fetch_batch(
                client, spec, base, headers, filter_strs[0], top, orderby, select
            )
            if err is not None:
                return err
            records = recs or []
        else:
            sem = asyncio.Semaphore(_MAX_CONCURRENCY)

            async def run(fs: str):
                async with sem:
                    return await _fetch_batch(
                        client, spec, base, headers, fs, top, orderby, select
                    )

            for recs, err, cap in await asyncio.gather(
                *(run(fs) for fs in filter_strs)
            ):
                if err is not None:
                    return err
                records.extend(recs or [])
                hit_cap = hit_cap or cap

            # Each batch sorted only its own slice, and `top` applies to the
            # whole result rather than to each request.
            if orderby:
                records = _sort_records(records, orderby)
            if len(records) > top:
                records = records[:top]
                hit_cap = True

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
    # Full projected rows (bounded) for the deterministic table/download layer.
    display_rows = [{k: r.get(k) for k in projection} for r in records[:1000]]
    aggregations = _aggregate(spec, records)

    result: dict[str, Any] = {
        "ok": True,
        "entity": entity,
        "filtersApplied": {k: v for k, v in filters.items() if v not in (None, "")},
        "rowsReturned": len(records),
        "rowsCapAt": top,
        # True only when the SOURCE still had rows at the cap — not merely when
        # the count happens to equal it. The planner relies on this to know a
        # chained step carried a partial key set forward.
        "truncated": hit_cap,
        "sample": sample,
        "rows": display_rows,
    }
    if aggregations:
        result.update(aggregations)

    # Group-by aggregation over ALL records (computed in code, never in OData).
    if group_by:
        buckets: dict[Any, dict[str, float]] = {}
        for r in records:
            key = r.get(group_by)
            if key in (None, ""):
                continue
            b = buckets.setdefault(key, {"count": 0, "amount": 0.0})
            b["count"] += 1
            if aggregate == "sum" and aggregate_column:
                b["amount"] += float(r.get(aggregate_column) or 0)
        sort_key = "amount" if aggregate == "sum" else "count"
        groups = sorted(
            ({"key": k, "count": int(v["count"]), "amount": round(v["amount"], 2)} for k, v in buckets.items()),
            key=lambda g: -g[sort_key],
        )
        result["groups"] = groups
        result["groupBy"] = group_by
        result["aggregate"] = aggregate or "count"
        if aggregate_column:
            result["aggregateColumn"] = aggregate_column

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
