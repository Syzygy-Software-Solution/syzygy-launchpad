"""Graph-driven query planner.

Replaces the hand-written one-directional traversal prose with a generic
plan-execute-summarize loop:

  1. PLAN      - one LLM call turns the user's question into an ordered plan of
                 entity queries (or a clarifying question). The prompt is built
                 from the agent's catalog cards + the relevant slice of the join
                 graph, so the model is grounded in real, bidirectional join
                 metadata instead of memorised forward-only rules.
  2. EXECUTE   - a DETERMINISTIC Python executor runs each step, substituting
                 values produced by earlier steps ($references). The LLM never
                 sees or invents surrogate keys — the executor carries them.
  3. SUMMARISE - one LLM call writes the final prose answer from the collected
                 step results.

This is what makes reverse queries ("all payments by payee X") as reliable as
forward ones: the executor follows whatever path the graph provides, in any
direction, and only the executor (not the model) handles the key values.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .agents.base import Agent, AgentRunResult
from .aicore_client import chat_completion
from .catalog import catalog, join_graph
from .tools.query_entity import query_entity

log = logging.getLogger(__name__)

MAX_PLAN_STEPS = 8


# ---------------------------------------------------------------------------
# Plan tool spec (advertised to the planner LLM)
# ---------------------------------------------------------------------------
# Flat schema on purpose: AI Core rejects top-level anyOf/oneOf/enum, so we keep
# the shape simple and validate semantics in Python.
RUN_PLAN_TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_query_plan",
        "description": (
            "Execute an ordered, multi-step data-retrieval plan across catalog "
            "entities. Steps run top-to-bottom; a later step may reference any "
            "column returned by an earlier step using \"$<id>.<COLUMN>\". Use "
            "this whenever the question requires fetching data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": "Ordered list of query steps.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Unique step id, e.g. 's1'.",
                            },
                            "entity": {
                                "type": "string",
                                "description": "Entity to query (must be one listed in the prompt).",
                            },
                            "filters": {
                                "type": "object",
                                "additionalProperties": True,
                                "description": (
                                    "Filter map for this entity. A value may be a "
                                    "literal, or \"$<id>.<COLUMN>\" to reference a "
                                    "column returned by an earlier step (e.g. "
                                    "\"$s1.PAYEESEQ\"). Never invent surrogate-key "
                                    "numbers."
                                ),
                            },
                            "top": {
                                "type": "integer",
                                "description": "Optional max rows for this step.",
                            },
                            "orderby": {
                                "type": "string",
                                "description": (
                                    "Optional OData $orderby, e.g. 'VALUE desc'. "
                                    "Required for top-N / highest / lowest asks."
                                ),
                            },
                        },
                        "required": ["id", "entity", "filters"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["steps"],
            "additionalProperties": False,
        },
    },
}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
def _build_planner_prompt(agent: Agent) -> str:
    from .catalog import render_catalog_for_prompt  # local import avoids cycle

    entities = agent.entities or list(catalog())
    cards = render_catalog_for_prompt(entities)
    graph = join_graph().render_for_prompt(entities)

    return f"""\
You are the QUERY PLANNER for the "{agent.name}" assistant over SAP Incentive
Management (SAP Commissions) data. Turn the user's question into an ordered PLAN
of entity queries. You do NOT write the final answer here — a separate step does.

# Tool
Call `run_query_plan(steps)` with the plan. Each step queries ONE entity.

# Entities you may query
{cards}

# How entities are connected (join graph)
These edges tell you WHICH column links two entities. Traverse them in EITHER
direction — the graph is symmetric.
{graph}

# Planning rules
- A step is {{id, entity, filters, [top], [orderby]}}. Give every step a short
  id (s1, s2, ...).
- Steps run in order. EVERY column a step returns is automatically available to
  later steps as "$<id>.<COLUMN>" — for example "$s1.PAYEESEQ" or
  "$s2.PERIODSEQ". Use these references to feed a value from one step into the
  next. You do NOT declare or name extracts; just reference the producing step's
  column.
- You do NOT know surrogate-key values (PERIODSEQ, PAYEESEQ, PERIODTYPESEQ, ...).
  NEVER write a literal numeric id. Reference the step + column that produced it.
- Reference the JOIN column of the edge you traverse (see the join graph): e.g.
  to go cs_participant -> cs_payment, use "$<participant step id>.PAYEESEQ".
- Every step must include at least one of that entity's required filters.

# Determining direction (the whole point)
Identify the TARGET entity (what the user wants rows OF) and the ANCHOR (what the
user gave you). Find the path anchor -> target in the graph and make one step
per hop, referencing "$<id>.<COLUMN>" to carry the join key forward. Examples:
- "all payments by payee John Smith":
    s1 cs_participant {{lastname: "Smith"}}
    s2 cs_payment    {{payeeseq_in: "$s1.PAYEESEQ"}}
- "payments in Feb 2022 (monthly)":
    s1 cs_periodtype {{name: "month"}}
    s2 cs_period {{periodtypeseq: "$s1.PERIODTYPESEQ", startdate_gte: "...", startdate_lt: "..."}}
    s3 cs_payment {{periodseq_in: "$s2.PERIODSEQ"}}
  If you will also list individual payments in the answer, just query the
  payments — payee names and positions are added automatically AFTER the query.
  Do NOT add cs_participant / cs_position / cs_title steps for a payment listing.
- Literal filter given (earning code/group, or a bare 16+ digit periodseq) and
  NO date phrase -> a single cs_payment step with that filter.
- "position of payee John Smith":
    s1 cs_participant {{lastname: "Smith"}}
    s2 cs_position {{payeeseq_in: "$s1.PAYEESEQ"}}
    s3 cs_title {{ruleelementownerseq_in: "$s2.TITLESEQ"}}
  (Positions/titles are effective-dated; if a payee has several, use the first.)
- "payments for position <title>":
    s1 cs_title {{name: "<title>"}}
    s2 cs_position {{titleseq_in: "$s1.RULEELEMENTOWNERSEQ"}}
    s3 cs_payment {{positionseq_in: "$s2.RULEELEMENTOWNERSEQ"}}

# Date handling (period resolution)
When the question has a date phrase, resolve the period first:
- Filter cs_period on STARTDATE only. Never use enddate_* — this view's ENDDATE
  uses a first-of-next-period convention and returns zero rows for windows.
- Pair startdate_gte (inclusive lower) with startdate_lt (EXCLUSIVE upper).
- Use ISO 8601 UTC with trailing Z, e.g. "2022-02-01T00:00:00Z". Use the
  "Today's date (UTC)" note for relative phrases. Month periods start day 1;
  quarters on Jan/Apr/Jul/Oct 1; years on Jan 1. Treat a user end date as
  exclusive ("to March 1" -> startdate_lt 2022-03-01T00:00:00Z).
- For a contiguous range spanning several periods (e.g. "Feb to March 2022"),
  use ONE cs_period step whose window covers the whole range (startdate_gte at
  the first period's start, startdate_lt just after the last period's start).
  Its "$<id>.PERIODSEQ" then carries ALL periods in the range to cs_payment. Do
  NOT make one cs_period call per month.

# Clarify instead of guessing (period type)
If the question contains a date phrase but NO period-type word (monthly,
quarterly, yearly, weekly, daily), DO NOT plan. Reply in plain text with exactly:
"Should I look at monthly, quarterly, or yearly periods?"
Wait for the answer. If the user says "you decide" / "just pick one", use month.
A literal periodseq or an earning code/group does NOT exempt this rule when a
date phrase is present.

# Clarify when a person is referenced without a name or id
If the question needs to identify a person/payee but gives NO name and NO
PAYEESEQ (e.g. "what is his position?"), do NOT plan. Reply in plain text asking
for the person's first or last name, and wait for the answer.

# Top-N / highest / lowest
Set orderby (e.g. "VALUE desc") and top=N on the final data step. Never rely on
row order without orderby.

# Field discipline
EARNINGGROUPID != EARNINGCODEID ("group" vs "code" are load-bearing).
PERIODSEQ (a period) != PERIODTYPESEQ (a kind of period).
The POSITION / TITLE shown to a user is cs_title.NAME (e.g. "Account Executive").
cs_position.NAME is an internal code (often a userid) and is NOT projected —
never report it as the position. Any question about a person's position or title
MUST end with a cs_title step and answer from cs_title.NAME.

# When no fetch is needed
For greetings or questions unrelated to the data, reply in plain text and do NOT
call the tool.
"""


_SUMMARISER_PROMPT = """\
You are the ANSWER WRITER. You are given a JSON object with:
- question:   the user's question.
- steps:      the executed query plan results (one block per step, in order).
              Each cs_payment block includes `periods` = the period name(s) it
              covers.
- enrichment: deterministic lookup maps you MUST use for display names:
                payeeName     : PAYEESEQ    -> "First Last"
                positionTitle : POSITIONSEQ -> position title (from cs_title)
Write the final reply to the user.

# Rules
- Use ONLY values present in the input. Never invent IDs, amounts, or names.
- Plain prose. No markdown tables, headings, bold, or blockquotes.
- Name the filter(s) / period(s) applied. For date-range answers, state the
  period TYPE (monthly / quarterly / yearly).
- State the row count (rowsReturned) and, when present, the total (totalValue).
  NEVER add a currency symbol to VALUE.
- If a payment block's `truncated` is true, say not all rows were counted.
- Report EACH cs_payment step SEPARATELY, labelled by its `periods`. A block with
  rowsReturned > 0 HAS data — never say a period returned no rows when its block
  shows rowsReturned > 0.

# Listing rows (ONLY if the user asked for "top N", "show N", "a few",
# "sample", or "list them")
List that many rows from the block's `sample` as plain bullet lines. For each row
resolve display names from the enrichment maps:
  PAYEE    = enrichment.payeeName[PAYEESEQ]      (else "(unknown)")
  POSITION = enrichment.positionTitle[POSITIONSEQ]  (else "(unknown)")
Format each line exactly:
  - PAYMENTSEQ <id>, PAYEE <name> (<PAYEESEQ>), POSITION <title>,
    EARNINGGROUPID <g>, EARNINGCODEID <c>, VALUE <v>
If the user did NOT ask to list rows, do NOT list individual rows.

# Position-of-a-person questions
State the person and their position title from the cs_title step's NAME (the
position is ALWAYS cs_title.NAME, never cs_position.NAME). If the person holds
multiple positions, use the first. If there is no cs_title step or it returned no
rows, say the position could not be resolved.

# Errors / empties
If a step returned ok:false, explain briefly using its `message`. If a data step
returned rowsReturned:0, say so plainly for that period only.
"""


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------
def _lookup_variable(name: str, variables: dict[str, list[Any]]) -> list[Any]:
    """Resolve a reference name to its extracted values.

    Preferred form is "<id>.<COLUMN>" (exact key). As a forgiving fallback for
    when the planner writes a bare "$COLUMN", match any step's column of that
    name and return the LATEST step's values (avoids surprises when the same
    column name — e.g. RULEELEMENTOWNERSEQ — exists in multiple steps).
    """
    if name in variables:
        return variables[name]
    target = name.split(".", 1)[-1].upper()

    def _step_num(key: str) -> int:
        prefix = key.split(".", 1)[0]
        digits = "".join(ch for ch in prefix if ch.isdigit())
        return int(digits) if digits else 0

    matches = [k for k in variables if k.split(".", 1)[-1].upper() == target]
    if not matches:
        return []
    matches.sort(key=_step_num)
    return variables[matches[-1]]


def _expand_value(
    value: Any, variables: dict[str, list[Any]]
) -> tuple[list[Any] | None, str | None]:
    """Expand a filter value into a flat list of concrete values.

    Handles three shapes the planner LLM produces:
      - a "$<id>.<COLUMN>" (or bare "$COLUMN") reference -> the extracted values
      - a list mixing literals and references -> each element expanded, flattened
      - a plain literal          -> a single-element list

    Returns (values, error); `error` is set when a referenced variable is empty.
    """
    if isinstance(value, str) and value.startswith("$"):
        name = value[1:]
        vals = _lookup_variable(name, variables)
        if not vals:
            return None, f"upstream step produced no '{name}' values"
        return list(vals), None
    if isinstance(value, (list, tuple)):
        out: list[Any] = []
        for item in value:
            sub, err = _expand_value(item, variables)
            if err:
                return None, err
            out.extend(sub)
        return out, None
    return [value], None


def _resolve_filters(
    entity: str,
    raw_filters: dict[str, Any],
    variables: dict[str, list[Any]],
) -> tuple[dict[str, Any], str | None]:
    """Substitute $references (including nested in lists) and coerce to the op.

    Returns (resolved_filters, error). `error` is set when a referenced variable
    is empty (upstream produced no rows), so the executor can short-circuit.

    Coercion is applied to EVERY value: the planner LLM sometimes passes a scalar
    to an `in` filter, a list to a scalar filter, or a list of "$ref" strings.
    Rather than fail the whole query, we reshape the value to what the operator
    expects.
    """
    spec = catalog().get(entity)
    resolved: dict[str, Any] = {}
    for key, value in (raw_filters or {}).items():
        expanded, err = _expand_value(value, variables)
        if err:
            return {}, err

        fspec = spec.filters.get(key) if spec else None
        if fspec is not None and fspec.op == "in":
            resolved[key] = expanded            # list for `in`
        elif fspec is not None:
            resolved[key] = expanded[0] if expanded else None  # scalar op
        else:
            # unknown filter key: keep shape natural so query_entity's error
            # message is about the unknown key, not a type mismatch.
            resolved[key] = expanded[0] if len(expanded) == 1 else expanded

    return resolved, None


async def _execute_plan(
    agent: Agent, steps: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Run each plan step in order, carrying extracts forward. Returns a trace."""
    variables: dict[str, list[Any]] = {}
    trace: list[dict[str, Any]] = []
    allowed = set(agent.entities or list(catalog()))

    for step in steps[:MAX_PLAN_STEPS]:
        step_id = step.get("id") or f"s{len(trace) + 1}"
        entity = step.get("entity") or ""
        raw_filters = step.get("filters") or {}

        if entity not in allowed:
            result = {
                "ok": False,
                "error": "entity_not_allowed",
                "message": (
                    f"Entity '{entity}' is not available to this agent. "
                    f"Allowed: {sorted(allowed)}."
                ),
            }
            trace.append({"name": "query_entity", "arguments": {"entity": entity, "filters": raw_filters}, "result": result, "step": step_id})
            break  # a broken hop invalidates everything downstream

        resolved_filters, feed_err = _resolve_filters(entity, raw_filters, variables)
        if feed_err:
            log.warning(
                "planner feed_err step=%s entity=%s raw_filters=%s available_vars=%s",
                step_id, entity, raw_filters, sorted(variables),
            )
            result = {"ok": False, "error": "no_upstream_values", "message": feed_err}
            trace.append({"name": "query_entity", "arguments": {"entity": entity, "filters": raw_filters}, "result": result, "step": step_id})
            break

        # Auto-extract EVERY projected column so later steps can reference
        # "$<id>.<COLUMN>" without the planner declaring extracts. Values are
        # computed over ALL records (not just the sample), so feed-forward is
        # complete even for large result sets.
        spec = catalog().get(entity)
        extract_cols = list(spec.projection) if (spec and spec.projection) else None
        result = await query_entity(
            entity=entity,
            filters=resolved_filters,
            top=int(step["top"]) if step.get("top") else 200,
            orderby=step.get("orderby"),
            extract_columns=extract_cols,
        )
        trace.append({
            "name": "query_entity",
            "arguments": {"entity": entity, "filters": resolved_filters, **({"orderby": step["orderby"]} if step.get("orderby") else {})},
            "result": result,
            "step": step_id,
        })

        if not result.get("ok"):
            break  # do not chain past a failed step

        # Register every extracted column under "<id>.<COLUMN>".
        for col, vals in (result.get("extracts") or {}).items():
            variables[f"{step_id}.{col}"] = vals

    return trace


# ---------------------------------------------------------------------------
# Deterministic FK-to-label enrichment (card-driven, not the LLM)
# ---------------------------------------------------------------------------
def _distinct(values: Any) -> list[Any]:
    out: list[Any] = []
    seen: set[Any] = set()
    for v in values:
        if v in (None, "") or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _in_filter_key(entity: str, column: str) -> str | None:
    """Find the caller-facing filter key for `column` on `entity` (prefer `in`)."""
    spec = catalog().get(entity)
    if not spec:
        return None
    eq_key: str | None = None
    for key, f in spec.filters.items():
        if f.column == column:
            if f.op == "in":
                return key
            if f.op == "eq" and eq_key is None:
                eq_key = key
    return eq_key


async def _resolve_lookup(
    via: str,
    match: str,
    display: list[str] | None,
    then: dict[str, Any] | None,
    source_values: list[Any],
) -> dict[Any, str]:
    """Resolve `source_values` through (via, match) to a label map.

    Leaf: returns {match_value -> "display fields joined"}.
    Two-hop (`then` set): {match_value -> label resolved via the next hop}.
    """
    if not source_values:
        return {}
    key = _in_filter_key(via, match)
    if not key:
        return {}
    res = await query_entity(entity=via, filters={key: list(source_values)}, top=1000)
    rows = res.get("sample") or []

    if not then:
        out: dict[Any, str] = {}
        for r in rows:
            label = " ".join(
                str(r.get(d, "")).strip() for d in (display or [])
            ).strip()
            out[r.get(match)] = label
        return out

    # Two-hop: map source -> intermediate key, then resolve the intermediate.
    inter_col = then["column"]
    src_to_inter: dict[Any, Any] = {}
    for r in rows:
        src_to_inter[r.get(match)] = r.get(inter_col)
    deeper = await _resolve_lookup(
        via=then["via"],
        match=then["match"],
        display=then.get("display"),
        then=then.get("then"),
        source_values=_distinct(src_to_inter.values()),
    )
    return {src: deeper.get(iv, "") for src, iv in src_to_inter.items()}


async def _auto_enrich(trace: list[dict[str, Any]]) -> dict[str, dict[Any, str]]:
    """Build label maps for every entity in the trace that declares enrichments.

    Never raises: enrichment is best-effort and must not break the answer.
    """
    maps: dict[str, dict[Any, str]] = {}
    for entry in trace:
        res = entry.get("result") or {}
        if not res.get("ok"):
            continue
        entity = (entry.get("arguments") or {}).get("entity")
        spec = catalog().get(entity or "")
        if not spec or not spec.enrichments:
            continue
        sample = res.get("sample") or []
        if not sample:
            continue
        for rule in spec.enrichments:
            try:
                source_values = _distinct(r.get(rule["column"]) for r in sample)
                if not source_values:
                    continue
                label_map = await _resolve_lookup(
                    via=rule["via"],
                    match=rule["match"],
                    display=rule.get("display"),
                    then=rule.get("then"),
                    source_values=source_values,
                )
                maps.setdefault(rule["as"], {}).update(label_map)
            except Exception:  # noqa: BLE001 - enrichment must never break output
                log.exception("enrichment rule %s failed", rule.get("as"))
    return maps


def _period_name_index(trace: list[dict[str, Any]]) -> dict[Any, str]:
    """PERIODSEQ -> NAME, gathered from every cs_period step in the trace."""
    idx: dict[Any, str] = {}
    for entry in trace:
        if (entry.get("arguments") or {}).get("entity") != "cs_period":
            continue
        for r in (entry.get("result") or {}).get("sample") or []:
            if r.get("PERIODSEQ") is not None:
                idx[r["PERIODSEQ"]] = r.get("NAME")
    return idx


# ---------------------------------------------------------------------------
# Summariser input shaping
# ---------------------------------------------------------------------------
def _shape_for_summary(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact each step result to what the summariser needs (drop extracts).

    cs_payment blocks are annotated with the human-readable period name(s) they
    cover, so the summariser can label each block (Feb vs March) unambiguously.
    """
    period_names = _period_name_index(trace)
    blocks: list[dict[str, Any]] = []
    for entry in trace:
        res = entry.get("result") or {}
        entity = entry.get("arguments", {}).get("entity")
        block = {"step": entry.get("step"), "entity": entity, "ok": res.get("ok")}
        if res.get("ok"):
            block.update({
                "filtersApplied": res.get("filtersApplied"),
                "rowsReturned": res.get("rowsReturned"),
                "truncated": res.get("truncated"),
                "sample": res.get("sample"),
            })
            for k in ("totalValue", "byEarningGroup", "byEarningCode"):
                if k in res:
                    block[k] = res[k]
            # Label payment blocks with the period name(s) they cover.
            if entity == "cs_payment":
                seqs = _distinct(
                    r.get("PERIODSEQ") for r in (res.get("sample") or [])
                )
                names = _distinct(
                    period_names.get(s) for s in seqs if period_names.get(s)
                )
                if names:
                    block["periods"] = names
        else:
            block.update({"error": res.get("error"), "message": res.get("message")})
        blocks.append(block)
    return blocks


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------
async def run_planner_agent(
    agent: Agent, user_messages: list[dict[str, Any]]
) -> AgentRunResult:
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # --- 1. PLAN ---
    plan_messages: list[dict[str, Any]] = [
        {"role": "system", "content": _build_planner_prompt(agent)},
        {"role": "system", "content": f"Today's date (UTC) is {today_utc}."},
        *user_messages,
    ]
    plan_resp = await chat_completion(
        messages=plan_messages,
        tools=[RUN_PLAN_TOOL_SPEC],
        tool_choice="auto",
        temperature=agent.temperature,
        max_tokens=1024,
    )
    choice = (plan_resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    tool_calls = msg.get("tool_calls") or []

    # No tool call => the planner answered directly (clarification / chat).
    if not tool_calls:
        return AgentRunResult(reply=(msg.get("content") or "").strip(), tool_calls=[])

    # Parse the plan from the first run_query_plan call.
    steps: list[dict[str, Any]] = []
    for call in tool_calls:
        fn = call.get("function") or {}
        if fn.get("name") == "run_query_plan":
            try:
                args = json.loads(fn.get("arguments") or "{}")
                steps = args.get("steps") or []
            except json.JSONDecodeError:
                steps = []
            break

    if not steps:
        return AgentRunResult(
            reply="I could not build a valid query plan for that. Please rephrase.",
            tool_calls=[],
        )

    log.info("planner PLAN=%s", json.dumps(steps, default=str))

    # --- 2. EXECUTE ---
    trace = await _execute_plan(agent, steps)

    # --- 2b. ENRICH (deterministic FK -> label, not the LLM) ---
    enrichment = await _auto_enrich(trace)

    # --- 3. SUMMARISE ---
    last_user = next(
        (m["content"] for m in reversed(user_messages) if m.get("role") == "user"),
        "",
    )
    summary_input = {
        "question": last_user,
        "steps": _shape_for_summary(trace),
        "enrichment": enrichment,
    }
    summary_messages = [
        {"role": "system", "content": _SUMMARISER_PROMPT},
        {"role": "system", "content": f"Today's date (UTC) is {today_utc}."},
        {"role": "user", "content": json.dumps(summary_input, default=str)},
    ]
    summary_resp = await chat_completion(
        messages=summary_messages,
        temperature=0.1,
        max_tokens=2000,
    )
    summary_msg = (summary_resp.get("choices") or [{}])[0].get("message", {})
    reply = (summary_msg.get("content") or "").strip()

    # Expose a clean tool trace (drop the internal 'step'/'extracts' noise).
    public_trace = [
        {"name": e["name"], "arguments": e["arguments"], "result": e["result"]}
        for e in trace
    ]
    return AgentRunResult(reply=reply, tool_calls=public_trace)
