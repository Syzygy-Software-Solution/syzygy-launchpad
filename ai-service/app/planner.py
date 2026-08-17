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
import re
from datetime import datetime, timezone
from typing import Any

from .agents.base import Agent, AgentRunResult
from .aicore_client import chat_completion
from .catalog import catalog, join_graph
from .tools.query_entity import query_entity

log = logging.getLogger(__name__)

# Full payment -> sales-transaction lineage is 6 hops; add period resolution
# (cs_periodtype + cs_period) at the front and optional detail steps for the
# intermediate stages and a legitimate plan reaches ~12 steps.
MAX_PLAN_STEPS = 14


def _usage(resp: dict[str, Any]) -> int:
    return int((resp.get("usage") or {}).get("total_tokens") or 0)


# Human-readable labels for the "Steps" panel (lookup/dimension entities).
_STEP_LABELS = {
    "cs_periodtype": "Identifying the period type",
    "cs_period": "Resolving the compensation period",
    "cs_participant": "Looking up participants",
    "cs_position": "Looking up positions",
    "cs_title": "Looking up position titles",
    "cs_depositincentivetrace": "Tracing deposits to incentives",
    "cs_incentivepmtrace": "Tracing incentives to measurements",
    "cs_pmcredittrace": "Tracing measurements to credits",
}


def _build_steps(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Friendly, ordered description of each executed query step."""
    steps: list[dict[str, Any]] = []
    for entry in trace:
        args = entry.get("arguments") or {}
        entity = args.get("entity") or ""
        res = entry.get("result") or {}
        spec = catalog().get(entity)
        ok = bool(res.get("ok"))

        if entity in _STEP_LABELS:
            label = _STEP_LABELS[entity]
        elif spec:
            noun = _entity_noun(spec)
            if res.get("groups") is not None:
                label = f"Ranking {noun} by {_group_header(spec, res.get('groupBy'))}"
            else:
                label = f"Fetching {noun}"
        else:
            label = f"Querying {entity}"

        if not ok:
            detail = res.get("message") or res.get("error") or "failed"
        elif res.get("groups") is not None:
            detail = f"{len(res.get('groups') or [])} groups"
        else:
            detail = f"{res.get('rowsReturned', 0):,} rows"

        steps.append({"label": label, "ok": ok, "detail": detail})
    return steps


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
                                    "Required for top-N / highest / lowest asks. "
                                    "NEVER put aggregate functions like count() "
                                    "here — use group_by/aggregate instead."
                                ),
                            },
                            "group_by": {
                                "type": "string",
                                "description": (
                                    "Column to group rows by for analytical / "
                                    "ranking questions (e.g. 'PAYEESEQ' for 'which "
                                    "payee has the most payments', 'EARNINGCODEID' "
                                    "for 'which earning code has the highest "
                                    "total'). Pair with aggregate and a high top."
                                ),
                            },
                            "aggregate": {
                                "type": "string",
                                "description": (
                                    "'count' (rows per group) or 'sum' (sum of a "
                                    "measure per group). Required with group_by."
                                ),
                            },
                            "aggregate_column": {
                                "type": "string",
                                "description": (
                                    "Measure column to sum when aggregate='sum' "
                                    "(e.g. 'VALUE')."
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

# Lineage: how a payment traces down to sales transactions
The calculation pipeline runs in this order, and each arrow is an EXACT link via
the entity named under it. This is the ONLY correct way to answer "what is
behind this payment", "which sales transactions produced this payment", "why was
this payment this amount", or the same question asked in reverse.

  payment -> deposit -> incentive -> measurement -> credit -> sales transaction
            (a)        (b)          (c)            (d)       (e)

  (a) payment -> deposit: no trace table. Filter cs_deposit on ALL of
      payeeseq_in / positionseq_in / periodseq_in / earninggroupid_in /
      earningcodeid_in, all fed from the payment step. A payment is the sum of
      the deposits sharing those five keys.
  (b) deposit -> incentive:    cs_depositincentivetrace (DEPOSITSEQ, INCENTIVESEQ)
  (c) incentive -> measurement: cs_incentivepmtrace     (INCENTIVESEQ, MEASUREMENTSEQ)
  (d) measurement -> credit:    cs_pmcredittrace        (MEASUREMENTSEQ, CREDITSEQ)
  (e) credit -> sales transaction: cs_credit.SALESTRANSACTIONSEQ

The three trace entities are PLUMBING. Query them to carry keys forward; do not
treat them as the answer. Traverse them in either direction — to go from a sales
transaction back up to the payments it contributed to, walk (e) to (a) backwards.

## The rule that matters
NEVER answer a lineage question by filtering the target entity on payeeseq /
periodseq / positionseq. That returns everything that payee did in that period —
NOT what is behind this record — and the totals will not reconcile. If the user
anchors on a SPECIFIC record, you MUST walk the chain hop by hop. Only use
payee/period filters when the user genuinely asked for a period-wide listing
("all credits in February").

## Worked example — "sales transactions behind this payment" (monthly)
    s1 cs_periodtype {{name: "month"}}
    s2 cs_period {{periodtypeseq: "$s1.PERIODTYPESEQ", startdate_gte: "2022-02-01T00:00:00Z", startdate_lt: "2022-03-01T00:00:00Z"}}
    s3 cs_payment {{periodseq_in: "$s2.PERIODSEQ"}} top=1
    s4 cs_deposit {{payeeseq_in: "$s3.PAYEESEQ", positionseq_in: "$s3.POSITIONSEQ", periodseq_in: "$s3.PERIODSEQ", earninggroupid_in: "$s3.EARNINGGROUPID", earningcodeid_in: "$s3.EARNINGCODEID"}}
    s5 cs_depositincentivetrace {{depositseq_in: "$s4.DEPOSITSEQ"}}
    s6 cs_incentivepmtrace {{incentiveseq_in: "$s5.INCENTIVESEQ"}}
    s7 cs_pmcredittrace {{measurementseq_in: "$s6.MEASUREMENTSEQ"}}
    s8 cs_credit {{creditseq_in: "$s7.CREDITSEQ"}}
    s9 cs_salestransaction {{salestransactionseq_in: "$s8.SALESTRANSACTIONSEQ"}}

Insert a cs_incentive / cs_measurement step only if the user asked to SEE those
stages; the chain does not need them to reach sales transactions.

## Reverse example — "which payment did this sales transaction end up in"
    s1 cs_salestransaction {{ponumber: "..."}}   (or productid, etc.)
    s2 cs_credit {{salestransactionseq_in: "$s1.SALESTRANSACTIONSEQ"}}
    s3 cs_pmcredittrace {{creditseq_in: "$s2.CREDITSEQ"}}
    s4 cs_incentivepmtrace {{measurementseq_in: "$s3.MEASUREMENTSEQ"}}
    s5 cs_depositincentivetrace {{incentiveseq_in: "$s4.INCENTIVESEQ"}}
    s6 cs_deposit {{depositseq_in: "$s5.DEPOSITSEQ"}}
    s7 cs_payment {{payeeseq_in: "$s6.PAYEESEQ", positionseq_in: "$s6.POSITIONSEQ", periodseq_in: "$s6.PERIODSEQ", earninggroupid: "$s6.EARNINGGROUPID", earningcodeid: "$s6.EARNINGCODEID"}}

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

# Analytical / ranking questions ("which X has the most/least Y", "top X by
# count/total", "who did the most", "highest total per X")
Do NOT put aggregate functions (count(), sum()) into orderby — the source
rejects them. Instead use ONE data step with group_by + aggregate:
- "which payee has the most payments in <period>":
    ...resolve the period...
    sN cs_payment {{periodseq_in: "$..PERIODSEQ"}} group_by="PAYEESEQ" aggregate="count"
- "which earning code has the highest total for <period>":
    sN cs_payment {{periodseq_in: "$..PERIODSEQ"}} group_by="EARNINGCODEID" aggregate="sum" aggregate_column="VALUE"
- "top positions by deposit amount": group_by="POSITIONSEQ" aggregate="sum" aggregate_column="VALUE"
The grouping/counting/ranking is done for you in code over all fetched rows.
group_by takes a COLUMN name (PAYEESEQ, EARNINGCODEID, POSITIONSEQ, ...).

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
You are the ANSWER WRITER. Input JSON:
- question:   the user's question.
- steps:      executed query results (one block per step, in order). Each block:
              entity, filtersApplied, rowsReturned, truncated, sample (array of
              row objects), optional totalValue / byEarningGroup / byEarningCode /
              byName, and (payment-style blocks) `periods` = period name(s).
- enrichment: label maps you MUST use:
              payeeName     : PAYEESEQ    -> "First Last"
              positionTitle : POSITIONSEQ -> position title

# DEFAULT ANSWER = one summary line + a table of rows
For ANY question that returned records (payments, deposits, incentives, credits,
measurements, sales orders/transactions, participants, ...), by default you:
  1. Write ONE summary line with the REAL numbers filled in.
  2. Render a Markdown pipe table LISTING the rows from the last data step's
     `sample` (up to 15 rows). Do NOT wait to be asked to "show" — list by
     default. Put the total at the end, not instead of the list.

The chat renders Markdown tables, **bold**, *italic*, `code`, and bullet lists.
It does NOT render '#' headings — never use them.

Summary line (fill from the data — count = rowsReturned, total = totalValue,
period = `periods`):
  "**{rowsReturned}** {noun} for **{period or filter}**. Total value: **{totalValue}**."

Then a blank line, then the table. Pick 4-7 columns that fit the entity, and
ALWAYS convert id columns to names:
  - a "Payee" column from enrichment.payeeName (fall back to the PAYEESEQ number)
  - a "Position" column from enrichment.positionTitle
Include the record id and the Value; add the entity's meaningful attributes
(earning group/code for payments & deposits & credits; NAME for
measurements/incentives; product for sales transactions). Worked example:

**239 payments** for **February to March 2022** (monthly). Total value: **461,941.37**.

| Payment | Payee | Position | Earning Group | Earning Code | Value |
|---|---|---|---|---|---|
| 26177172834093108 | Ian Irving | Account Executive | Commission | MBO | 625.00 |
| 26177172834093110 | Jane Reed | Sales Manager | Commission | SPIFF | 211.40 |

If `truncated` is true, add one line after the table:
  "Showing the first {number of rows in sample} of **{rowsReturned}** rows."

# Exceptions (do NOT force a table)
- If the user asked ONLY for a count ("how many"), reply with the number.
- If the user asked ONLY for a total, reply with the total.
- If the user asked for a breakdown ("by earning code/group", "by name"), render a
  small table from byEarningGroup / byEarningCode / byName: | Category | Count |
  Amount | and a bold total line.
- Position-of-a-person question: state the person and their title from the
  cs_title NAME (NEVER cs_position.NAME); if multiple, use the first.

# Lineage answers
When the plan walked the trace chain (steps on cs_depositincentivetrace,
cs_incentivepmtrace or cs_pmcredittrace are present), the user asked what is
BEHIND a record. Then:
- Do NOT render tables for the trace steps — they are surrogate-key plumbing.
  Report the stage entities (payment, deposit, credit, sales transaction).
- Say the records ARE the lineage of the anchor record, e.g. "The 34.96 payment
  traces to N sales transactions" — never "from the same period", which implies
  a fan-out rather than a traced link.
- If a downstream total does not match the anchor amount, that is expected
  (credits and transactions are gross amounts, the payment is the paid result).
  Do not present the two as if they should reconcile, and do not silently imply
  they do.

# Hard rules
- Fill EVERY figure from the data. NEVER output an empty "****" — if a value is
  genuinely absent, drop that clause rather than leave it blank.
- NEVER claim records are related because they share a payee or period. If the
  plan did not walk the trace chain, describe them as separate result sets.
- Use ONLY values present in the input; never invent ids, amounts, or names.
- No currency symbols. Use thousands separators for large numbers.
- Report EACH data step; a block with rowsReturned > 0 HAS data — never call it
  empty. Label payment blocks by their `periods`.
- If a step is ok:false, explain briefly using its `message`.
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

    planned = steps[:MAX_PLAN_STEPS]
    for idx, step in enumerate(planned):
        step_id = step.get("id") or f"s{len(trace) + 1}"
        entity = step.get("entity") or ""
        raw_filters = step.get("filters") or {}
        # A step whose keys feed a later step must not truncate: dropping rows
        # there silently narrows everything downstream.
        feeds_forward = idx < len(planned) - 1

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
        group_by = step.get("group_by")
        # Row cap, in priority order:
        #   1. an explicit `top` from the plan (top-N questions)
        #   2. the card's maxTop when the step aggregates over everything or
        #      feeds a later step — both need complete data to be correct
        #   3. the card's defaultTop for a final display step
        # Caps now come from the card (query.defaultTop / query.maxTop) rather
        # than the hardcoded 200/1000 they used to be.
        if step.get("top"):
            step_top = int(step["top"])
        elif group_by or feeds_forward:
            step_top = spec.max_top if spec else 1000
        else:
            step_top = spec.default_top if spec else 200
        result = await query_entity(
            entity=entity,
            filters=resolved_filters,
            top=step_top,
            orderby=step.get("orderby"),
            extract_columns=extract_cols,
            group_by=group_by,
            aggregate=step.get("aggregate"),
            aggregate_column=step.get("aggregate_column"),
        )
        # An explicit `top` in the plan is a DELIBERATE limit — "the first
        # payment", "top 5 by value". Filling it is the intended outcome, not
        # truncation, so it must not raise a partial-results warning.
        if result.get("ok") and step.get("top"):
            result["truncated"] = False

        # A truncated step that feeds a later one carried an INCOMPLETE key set
        # forward, so everything downstream is a subset of the true answer.
        # Flag it loudly rather than letting it pass as a complete result.
        if result.get("ok") and result.get("truncated") and feeds_forward:
            result["feedTruncated"] = True
            log.warning(
                "planner step=%s entity=%s TRUNCATED at %s rows while feeding "
                "later steps — downstream results are incomplete",
                step_id, entity, result.get("rowsCapAt"),
            )

        # Resolve group keys to human labels via the entity's enrichment rule.
        if result.get("ok") and result.get("groups") and group_by:
            try:
                result["groupLabels"] = await _resolve_group_labels(
                    entity, group_by, [g["key"] for g in result["groups"]]
                )
            except Exception:  # noqa: BLE001 - labels are best-effort
                log.exception("group label resolution failed")
        trace.append({
            "name": "query_entity",
            "arguments": {"entity": entity, "filters": resolved_filters, **({"orderby": step["orderby"]} if step.get("orderby") else {}), **({"group_by": group_by} if group_by else {})},
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
    # `rows` (up to 1000), not `sample` (50): a label map built from the sample
    # left every row past the 50th showing a raw surrogate key, in the table AND
    # in the CSV. Chunking in query_entity makes the wide `in` list safe.
    rows = res.get("rows") or res.get("sample") or []

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


def _default_display(spec: Any) -> list[str]:
    if "NAME" in spec.columns:
        return ["NAME"]
    for n in (spec.projection or []):
        c = spec.columns.get(n)
        if c and c.role == "attribute" and c.type == "string":
            return [n]
    return []


async def _resolve_group_labels(entity: str, group_by: str, keys: list[Any]) -> dict[Any, str]:
    """Resolve group-by key values (e.g. PAYEESEQ) to human labels."""
    spec = catalog().get(entity)
    if not spec:
        return {}
    for rule in spec.enrichments or []:
        if rule.get("column") == group_by:
            return await _resolve_lookup(
                rule["via"], rule["match"], rule.get("display"), rule.get("then"), keys
            )
    col = spec.columns.get(group_by)
    if col and col.references:
        via = col.references.get("entity")
        match = col.references.get("column", group_by)
        via_spec = catalog().get(via or "")
        if via_spec:
            display = _default_display(via_spec)
            if display:
                return await _resolve_lookup(via, match, display, None, keys)
    return {}


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
        # Enrich over every displayed row, not just the first 50.
        sample = res.get("rows") or res.get("sample") or []
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
        res = entry.get("result") or {}
        for r in res.get("rows") or res.get("sample") or []:
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
# Deterministic response rendering (code-driven, not the LLM)
# ---------------------------------------------------------------------------
# The answer's STRUCTURE lives here, not in a prompt, so it can't drift when we
# tweak wording. For any result whose target is a "fact" entity (has a measure
# column) we build the summary line + Markdown table ourselves. Non-fact answers
# (positions, single lookups, chat) fall back to the LLM summariser.
_COUNT_RE = re.compile(r"\bhow many\b|\bcount\b|\bnumber of\b", re.I)
_BREAKDOWN_RE = re.compile(
    r"break\s?down|\bby earning (code|group)\b|\bgroup by\b|\bby name\b|\bby group\b|\bby code\b",
    re.I,
)
_SHOWALL_RE = re.compile(
    r"\ball\s+(the\s+)?(rows|records|\d+)\b|\bshow (me )?all\b|\bevery (row|record)\b"
    r"|\bentire (list|table)\b|\bfull (list|table|data)\b|\beverything\b",
    re.I,
)
_CHART_RE = re.compile(
    r"\b(chart|graph|plot|pie|bar\s?chart|bar\s?graph|line\s?chart|line\s?graph"
    r"|visuali[sz]e|graphical(ly)?)\b",
    re.I,
)
_MAX_TABLE_ROWS = 15
_MAX_SHOWALL_ROWS = 300
_PALETTE = [
    "#6366f1", "#22c55e", "#f59e0b", "#ef4444", "#06b6d4", "#8b5cf6",
    "#ec4899", "#14b8a6", "#f97316", "#3b82f6", "#a3e635", "#eab308",
]


def _fmt_num(value: Any, decimals: int = 2) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "" if value is None else str(value)
    if f == int(f):
        return f"{int(f):,}"
    return f"{f:,.{decimals}f}"


def _fmt_money(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "" if value is None else str(value)


def _cell(text: Any) -> str:
    """Sanitise a value for a Markdown table cell."""
    s = "" if text is None else str(text)
    return s.replace("|", "\\|").replace("\n", " ").strip()


def _has_measure(spec: Any) -> bool:
    return any(c.role == "measure" for c in spec.columns.values())


def _is_trace_entity(spec: Any) -> bool:
    return getattr(spec, "kind", "fact") == "trace"


def _is_fact_entity(spec: Any) -> bool:
    # Trace cards carry a CONTRIBUTIONVALUE measure but are pure lineage
    # plumbing — rendering them would show the user a table of surrogate keys.
    # They are traversed for their keys; the stage entities either side of them
    # are what gets reported. (A trace that is the FINAL step is the answer
    # itself — see _fact_steps.)
    if _is_trace_entity(spec):
        return False
    return _has_measure(spec)


def _primary_fact_step(trace: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Last ok step whose entity is a fact table (has a measure)."""
    for entry in reversed(trace):
        res = entry.get("result") or {}
        if not res.get("ok"):
            continue
        spec = catalog().get((entry.get("arguments") or {}).get("entity") or "")
        if spec and _is_fact_entity(spec):
            return entry
    return None


def _enrich_get(enrichment: dict[str, dict[Any, str]], name: str, val: Any) -> str:
    m = enrichment.get(name) or {}
    label = m.get(val)
    if label is None:
        label = m.get(str(val))
    return label if label else str(val)


def _table_columns(spec: Any, row_keys: set[str]) -> list[tuple[str, str, str]]:
    """Return (kind, column, header) tuples for the table.

    Guarantees the primary measure (the amount) is always kept, then fills the
    remaining budget with id/payee/position/period + string attributes.
    """
    proj = spec.projection or list(spec.columns)
    ids: list[tuple[str, str, str]] = []

    pk = next((c.name for c in spec.columns.values() if c.role == "primary_key"), None)
    if pk and pk in row_keys:
        ids.append(("id", pk, spec.columns[pk].label or pk))
    if "PAYEESEQ" in row_keys:
        ids.append(("payee", "PAYEESEQ", "Payee"))
    if "POSITIONSEQ" in row_keys:
        ids.append(("position", "POSITIONSEQ", "Position"))
    if "PERIODSEQ" in row_keys:
        ids.append(("period", "PERIODSEQ", "Period"))

    attrs = [
        ("attr", n, spec.columns[n].label or n)
        for n in proj
        if (c := spec.columns.get(n)) and c.role == "attribute" and c.type == "string" and n in row_keys
    ]
    measures = [
        ("num", n, spec.columns[n].label or n)
        for n in proj
        if (c := spec.columns.get(n)) and c.role == "measure" and n in row_keys
    ]
    primary_measure = measures[:1]

    max_cols = 8
    budget = max_cols - len(ids) - len(primary_measure)
    combined = ids + attrs[: max(0, budget)] + primary_measure

    seen: set[str] = set()
    out = [c for c in combined if c[1] not in seen and not seen.add(c[1])]
    return out[:max_cols]


def _markdown_table(
    spec: Any,
    rows: list[dict[str, Any]],
    enrichment: dict[str, dict[Any, str]],
    period_names: dict[Any, str],
    max_rows: int = _MAX_TABLE_ROWS,
) -> str:
    row_keys: set[str] = set()
    for r in rows[:50]:
        row_keys.update(r.keys())
    cols = _table_columns(spec, row_keys)
    if not cols:
        return ""

    header = "| " + " | ".join(_cell(h) for _, _, h in cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    lines = [header, sep]
    for r in rows[:max_rows]:
        cells = []
        for kind, name, _ in cols:
            v = r.get(name)
            if kind == "payee":
                cells.append(_cell(_enrich_get(enrichment, "payeeName", v)))
            elif kind == "position":
                cells.append(_cell(_enrich_get(enrichment, "positionTitle", v)))
            elif kind == "period":
                cells.append(_cell(period_names.get(v) or v))
            elif kind == "num":
                cells.append(_cell(_fmt_money(v)))
            else:
                cells.append(_cell(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _breakdown_answer(
    question: str, res: dict[str, Any], noun: str, period_txt: str
) -> str | None:
    q = question.lower()
    if "group" in q and res.get("byEarningGroup"):
        agg, label = res["byEarningGroup"], "Earning Group"
    elif "code" in q and res.get("byEarningCode"):
        agg, label = res["byEarningCode"], "Earning Code"
    elif res.get("byName"):
        agg, label = res["byName"], "Name"
    elif res.get("byEarningCode"):
        agg, label = res["byEarningCode"], "Earning Code"
    elif res.get("byEarningGroup"):
        agg, label = res["byEarningGroup"], "Earning Group"
    else:
        return None

    ordered = sorted(agg.items(), key=lambda kv: -(kv[1].get("amount") or 0))
    lines = [f"| {label} | Count | Amount |", "|---|---:|---:|"]
    total = 0.0
    for key, v in ordered:
        cnt = v.get("count", 0)
        amt = v.get("amount", 0) or 0
        total += amt
        lines.append(f"| {_cell(key)} | {cnt:,} | {_fmt_money(amt)} |")
    head = f"Breakdown of {noun}" + (f" for **{period_txt}**" if period_txt else "") + ":"
    return head + "\n\n" + "\n".join(lines) + f"\n\n**Total: {_fmt_money(total)}**"


def _entity_noun(spec: Any) -> str:
    return (spec.domains[0] if spec.domains else spec.name.replace("cs_", "")).strip()


def _fact_steps(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """All ok steps whose entity is reportable, in execution order.

    A trace entity counts only when it is the LAST step — that means the user
    asked about the trace itself ("show the credit trace for measurement X")
    rather than passing through it on the way to sales transactions.
    """
    ok_steps = [e for e in trace if (e.get("result") or {}).get("ok")]
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(ok_steps):
        spec = catalog().get((entry.get("arguments") or {}).get("entity") or "")
        if not spec:
            continue
        if _is_fact_entity(spec):
            out.append(entry)
        elif _is_trace_entity(spec) and _has_measure(spec) and i == len(ok_steps) - 1:
            out.append(entry)
    return out


def _fact_table_block(
    entry: dict[str, Any],
    enrichment: dict[str, dict[Any, str]],
    period_names: dict[Any, str],
    header: bool,
    max_rows: int = _MAX_TABLE_ROWS,
) -> str:
    """Summary line + Markdown table for one fact step (header for multi-entity)."""
    res = entry["result"]
    spec = catalog().get(entry["arguments"]["entity"])
    rows = res.get("rows") or res.get("sample") or []
    rr = res.get("rowsReturned", len(rows))
    total = res.get("totalValue")
    noun = _entity_noun(spec)
    period_txt = ", ".join(
        _distinct(period_names.get(r.get("PERIODSEQ")) for r in rows if period_names.get(r.get("PERIODSEQ")))
    )

    if rr == 0:
        base = f"No {noun} found" + (f" for **{period_txt}**" if period_txt else "") + "."
        return f"**{noun.capitalize()}**\n\n{base}" if header else base

    if header:
        lead = f"**{noun.capitalize()}** — **{rr:,}** records"
        if total is not None:
            lead += f", total **{_fmt_money(total)}**"
    else:
        lead = f"**{rr:,}** {noun}"
        if period_txt:
            lead += f" for **{period_txt}**"
        if total is not None:
            lead += f". Total value: **{_fmt_money(total)}**"
    lead += "."

    table = _markdown_table(spec, rows, enrichment, period_names, max_rows)
    parts = [lead, "", table] if table else [lead]
    shown = min(len(rows), max_rows)
    if rr > shown:
        parts.append(f"\nShowing {shown} of {rr:,} rows — use the download button below for all rows.")
    # Distinct from the line above: there were MORE rows at the source than we
    # fetched, so any total shown covers only what was retrieved.
    if res.get("truncated"):
        parts.append(
            f"\n_Capped at {res.get('rowsCapAt', rr):,} rows — more exist upstream, "
            f"so the total above covers only the rows fetched._"
        )
    return "\n".join(parts)


def _single_fact_answer(
    question: str,
    entry: dict[str, Any],
    enrichment: dict[str, dict[Any, str]],
    period_names: dict[Any, str],
    max_rows: int = _MAX_TABLE_ROWS,
) -> str:
    res = entry["result"]
    spec = catalog().get(entry["arguments"]["entity"])
    rows = res.get("rows") or res.get("sample") or []
    rr = res.get("rowsReturned", len(rows))
    total = res.get("totalValue")
    noun = _entity_noun(spec)
    period_txt = ", ".join(
        _distinct(period_names.get(r.get("PERIODSEQ")) for r in rows if period_names.get(r.get("PERIODSEQ")))
    )

    if _COUNT_RE.search(question):
        # "at least" rather than an exact figure when the source had more rows
        # than we fetched — a capped count stated flatly is just wrong.
        s = f"There are **at least {rr:,}** {noun}" if res.get("truncated") else f"There are **{rr:,}** {noun}"
        if period_txt:
            s += f" for **{period_txt}**"
        if total is not None:
            s += f", totalling **{_fmt_money(total)}**"
        s += "."
        if res.get("truncated"):
            s += (
                f" This is capped at {res.get('rowsCapAt', rr):,} rows — more exist "
                f"upstream, so treat both figures as a lower bound."
            )
        return s

    if _BREAKDOWN_RE.search(question):
        bt = _breakdown_answer(question, res, noun, period_txt)
        if bt:
            return bt

    return _fact_table_block(entry, enrichment, period_names, header=False, max_rows=max_rows)


def _group_header(spec: Any, group_by: str) -> str:
    special = {"PAYEESEQ": "Payee", "POSITIONSEQ": "Position", "PERIODSEQ": "Period"}
    if group_by in special:
        return special[group_by]
    col = spec.columns.get(group_by)
    return col.label if col else group_by


def _group_label(
    entry: dict[str, Any], key: Any, period_names: dict[Any, str]
) -> str:
    labels = entry["result"].get("groupLabels") or {}
    lbl = labels.get(key) or labels.get(str(key))
    if lbl:
        return lbl
    if entry["result"].get("groupBy") == "PERIODSEQ":
        return period_names.get(key) or str(key)
    return str(key)


def _grouped_block(
    entry: dict[str, Any], period_names: dict[Any, str], header: bool
) -> str:
    res = entry["result"]
    spec = catalog().get(entry["arguments"]["entity"])
    groups = res.get("groups") or []
    group_by = res.get("groupBy")
    agg = res.get("aggregate", "count")
    noun = _entity_noun(spec)
    gh = _group_header(spec, group_by)

    if not groups:
        return f"No {noun} found."

    top = groups[0]
    top_lbl = _group_label(entry, top["key"], period_names)
    if agg == "sum":
        headline = f"**{top_lbl}** leads with **{_fmt_money(top['amount'])}** total across {top['count']:,} {noun}."
    else:
        headline = f"**{top_lbl}** leads with **{top['count']:,}** {noun}."
    if header:
        headline = f"**{noun.capitalize()} by {gh}** — " + headline

    lines = [f"| {gh} | Count | Total |", "|---|---:|---:|"]
    for g in groups[:15]:
        lines.append(
            f"| {_cell(_group_label(entry, g['key'], period_names))} | {g['count']:,} | {_fmt_money(g['amount'])} |"
        )
    tail = f"\nShowing the top {min(len(groups), 15)} of {len(groups):,}." if len(groups) > 15 else ""
    return headline + "\n\n" + "\n".join(lines) + tail


def _try_deterministic_answer(
    question: str,
    trace: list[dict[str, Any]],
    enrichment: dict[str, dict[Any, str]],
    period_names: dict[Any, str],
) -> str | None:
    """Build the reply for fact-entity results. None => let the LLM answer.

    Grouped (analytical) results render a ranked table; otherwise renders every
    fact step's rows, with count/breakdown shortcuts for single-entity questions.
    """
    fact_steps = _fact_steps(trace)
    if not fact_steps:
        return None

    grouped = [e for e in fact_steps if e["result"].get("groups")]
    if grouped:
        multi = len(grouped) > 1
        return "\n\n".join(_grouped_block(e, period_names, header=multi) for e in grouped)

    max_rows = _MAX_SHOWALL_ROWS if _SHOWALL_RE.search(question) else _MAX_TABLE_ROWS
    if len(fact_steps) == 1:
        body = _single_fact_answer(question, fact_steps[0], enrichment, period_names, max_rows)
    else:
        body = "\n\n".join(
            _fact_table_block(e, enrichment, period_names, header=True, max_rows=max_rows)
            for e in fact_steps
        )

    # An intermediate step that truncated fed an incomplete key set forward, so
    # every later step is a subset of the true answer. That has to be stated —
    # the rows shown look perfectly normal.
    if any((e.get("result") or {}).get("feedTruncated") for e in trace):
        body += (
            "\n\n_Note: an intermediate step hit its row cap, so these results "
            "are partial — narrow the question (a shorter period, a specific "
            "payee) for a complete answer._"
        )
    return body


# ---------------------------------------------------------------------------
# Datasets (for CSV download) + deterministic SVG charts (never LLM-drawn)
# ---------------------------------------------------------------------------
def _build_datasets(
    trace: list[dict[str, Any]],
    enrichment: dict[str, dict[Any, str]],
    period_names: dict[Any, str],
) -> list[dict[str, Any]]:
    """Structured, fully-enriched rows per fact entity, for CSV download."""
    datasets: list[dict[str, Any]] = []
    for entry in _fact_steps(trace):
        res = entry["result"]
        spec = catalog().get(entry["arguments"]["entity"])

        # Grouped (analytical) result -> a ranked dataset.
        if res.get("groups"):
            group_by = res.get("groupBy")
            gh = _group_header(spec, group_by)
            columns = [{"key": "group", "label": gh}, {"key": "count", "label": "Count"}, {"key": "total", "label": "Total"}]
            rows = [
                {
                    "group": _group_label(entry, g["key"], period_names),
                    "count": f"{g['count']:,}",
                    "total": _fmt_money(g["amount"]),
                }
                for g in res["groups"]
            ]
            datasets.append({
                "title": f"{_entity_noun(spec).capitalize()} by {gh}",
                "entity": spec.name,
                "columns": columns,
                "rows": rows,
                "count": len(rows),
            })
            continue

        all_rows = res.get("rows") or res.get("sample") or []
        if not all_rows:
            continue
        row_keys: set[str] = set()
        for r in all_rows[:50]:
            row_keys.update(r.keys())
        cols = _table_columns(spec, row_keys)
        columns = [{"key": name, "label": header} for _, name, header in cols]
        out_rows = []
        for r in all_rows:
            o: dict[str, Any] = {}
            for kind, name, _ in cols:
                v = r.get(name)
                if kind == "payee":
                    o[name] = _enrich_get(enrichment, "payeeName", v)
                elif kind == "position":
                    o[name] = _enrich_get(enrichment, "positionTitle", v)
                elif kind == "period":
                    o[name] = period_names.get(v) or ("" if v is None else str(v))
                elif kind == "num":
                    o[name] = _fmt_money(v)
                else:
                    o[name] = "" if v is None else str(v)
            out_rows.append(o)
        datasets.append({
            "title": _entity_noun(spec).capitalize(),
            "entity": spec.name,
            "columns": columns,
            "rows": out_rows,
            "count": res.get("rowsReturned", len(out_rows)),
        })
    return datasets


def _pick_breakdown(question: str, res: dict[str, Any]) -> tuple[str | None, dict | None]:
    q = question.lower()
    if "group" in q and res.get("byEarningGroup"):
        return "Earning Group", res["byEarningGroup"]
    if "code" in q and res.get("byEarningCode"):
        return "Earning Code", res["byEarningCode"]
    if res.get("byName"):
        return "Name", res["byName"]
    if res.get("byEarningCode"):
        return "Earning Code", res["byEarningCode"]
    if res.get("byEarningGroup"):
        return "Earning Group", res["byEarningGroup"]
    return None, None


def _build_charts(question: str, trace: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Backend-drawn SVG charts (deterministic) — only when a chart is asked for."""
    if not _CHART_RE.search(question):
        return []
    q = question.lower()
    ctype = "pie" if "pie" in q else ("line" if "line" in q else "bar")
    charts: list[dict[str, str]] = []
    # Chart the ranked groups when the question was analytical; otherwise chart
    # only the PRIMARY fact step. A lineage chain has several fact steps and
    # charting all of them buries the one the user actually asked about.
    steps = _fact_steps(trace)
    grouped = [e for e in steps if e["result"].get("groups")]
    if grouped:
        targets = grouped
    else:
        primary = _primary_fact_step(trace)
        targets = [primary] if primary else []
    for entry in targets:
        res = entry["result"]
        spec = catalog().get(entry["arguments"]["entity"])

        # Grouped result -> chart the ranked groups directly.
        if res.get("groups"):
            gh = _group_header(spec, res.get("groupBy"))
            use_amount = res.get("aggregate") == "sum"
            data = [
                (str(_group_label(entry, g["key"], {})), float(g["amount"] if use_amount else g["count"]))
                for g in res["groups"][:12]
            ]
            data = [d for d in data if d[1] != 0] or data
            if data:
                title = f"{_entity_noun(spec).capitalize()} by {gh}"
                charts.append({"title": title, "svg": _svg_chart(ctype, title, data)})
            continue

        label, agg = _pick_breakdown(question, res)
        if not agg:
            continue
        data = [(str(k), float(v.get("amount") or 0)) for k, v in agg.items()]
        data = [d for d in data if d[1] != 0] or data
        data.sort(key=lambda x: -x[1])
        data = data[:12]
        if not data:
            continue
        title = f"{_entity_noun(spec).capitalize()} by {label}"
        charts.append({"title": title, "svg": _svg_chart(ctype, title, data)})
    return charts


def _xml_escape(text: str) -> str:
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _svg_chart(kind: str, title: str, data: list[tuple[str, float]]) -> str:
    if kind == "pie":
        return _svg_pie(title, data)
    if kind == "line":
        return _svg_line(title, data)
    return _svg_bar(title, data)


def _svg_open(w: int, h: int, title: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="100%" style="max-width:{w}px;height:auto">',
        f'<text x="{w/2}" y="20" text-anchor="middle" font-size="14" '
        f'font-weight="600" fill="#1e293b">{_xml_escape(title)}</text>',
    ]


def _svg_bar(title: str, data: list[tuple[str, float]]) -> str:
    W, H, pl, pb, pt, pr = 540, 320, 55, 80, 34, 12
    n = len(data)
    maxv = max(v for _, v in data) or 1
    plot_w, plot_h = W - pl - pr, H - pt - pb
    gap = plot_w / n
    bw = gap * 0.6
    p = _svg_open(W, H, title)
    p.append(f'<line x1="{pl}" y1="{pt}" x2="{pl}" y2="{H-pb}" stroke="#cbd5e1"/>')
    p.append(f'<line x1="{pl}" y1="{H-pb}" x2="{W-pr}" y2="{H-pb}" stroke="#cbd5e1"/>')
    for i, (name, val) in enumerate(data):
        x = pl + gap * i + (gap - bw) / 2
        bh = plot_h * (val / maxv)
        y = (H - pb) - bh
        color = _PALETTE[i % len(_PALETTE)]
        p.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{color}" rx="2"/>')
        p.append(f'<text x="{x+bw/2:.1f}" y="{y-4:.1f}" text-anchor="middle" font-size="10" fill="#334155">{_xml_escape(_fmt_money(val))}</text>')
        cx = x + bw / 2
        p.append(f'<text x="{cx:.1f}" y="{H-pb+14:.1f}" text-anchor="end" font-size="10" fill="#475569" transform="rotate(-35 {cx:.1f} {H-pb+14:.1f})">{_xml_escape(name[:18])}</text>')
    p.append("</svg>")
    return "".join(p)


def _svg_line(title: str, data: list[tuple[str, float]]) -> str:
    W, H, pl, pb, pt, pr = 540, 320, 55, 80, 34, 12
    n = len(data)
    maxv = max(v for _, v in data) or 1
    plot_w, plot_h = W - pl - pr, H - pt - pb
    step = plot_w / max(1, n - 1) if n > 1 else plot_w
    p = _svg_open(W, H, title)
    p.append(f'<line x1="{pl}" y1="{pt}" x2="{pl}" y2="{H-pb}" stroke="#cbd5e1"/>')
    p.append(f'<line x1="{pl}" y1="{H-pb}" x2="{W-pr}" y2="{H-pb}" stroke="#cbd5e1"/>')
    pts = []
    for i, (name, val) in enumerate(data):
        x = pl + step * i
        y = (H - pb) - plot_h * (val / maxv)
        pts.append(f"{x:.1f},{y:.1f}")
        p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#6366f1"/>')
        p.append(f'<text x="{x:.1f}" y="{y-6:.1f}" text-anchor="middle" font-size="9" fill="#334155">{_xml_escape(_fmt_money(val))}</text>')
        p.append(f'<text x="{x:.1f}" y="{H-pb+14:.1f}" text-anchor="end" font-size="10" fill="#475569" transform="rotate(-35 {x:.1f} {H-pb+14:.1f})">{_xml_escape(name[:18])}</text>')
    p.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="#6366f1" stroke-width="2"/>')
    p.append("</svg>")
    return "".join(p)


def _svg_pie(title: str, data: list[tuple[str, float]]) -> str:
    import math

    W, H, cx, cy, r = 540, 340, 160, 190, 115
    total = sum(v for _, v in data) or 1
    p = _svg_open(W, H, title)
    ang = -math.pi / 2
    for i, (name, val) in enumerate(data):
        frac = val / total
        a2 = ang + frac * 2 * math.pi
        x1, y1 = cx + r * math.cos(ang), cy + r * math.sin(ang)
        x2, y2 = cx + r * math.cos(a2), cy + r * math.sin(a2)
        large = 1 if frac > 0.5 else 0
        color = _PALETTE[i % len(_PALETTE)]
        p.append(f'<path d="M{cx},{cy} L{x1:.1f},{y1:.1f} A{r},{r} 0 {large} 1 {x2:.1f},{y2:.1f} Z" fill="{color}"/>')
        ly = 70 + i * 22
        p.append(f'<rect x="330" y="{ly-11}" width="12" height="12" fill="{color}" rx="2"/>')
        p.append(f'<text x="348" y="{ly}" font-size="11" fill="#334155">{_xml_escape(name[:18])} ({frac*100:.0f}%)</text>')
        ang = a2
    p.append("</svg>")
    return "".join(p)


# ---------------------------------------------------------------------------
# Narrator — a warm intro + a proactive follow-up around deterministic tables
# ---------------------------------------------------------------------------
_NARRATOR_PROMPT = """\
You add a warm, human, colleague-like tone around a data answer that is ALREADY
shown to the user as tables (you cannot see the tables). Input JSON:
- question: what the user asked.
- payee:    the person the data is about (may be null).
- shown:    record types already displayed, each {type, count, total}.
- related:  OTHER record types available to explore that were NOT shown.
- lineage:  true when the records were reached by TRACING the calculation chain
            (payment -> deposit -> incentive -> measurement -> credit -> sales
            transaction), false when they were fetched independently.
- chain:    the stages traversed, in order (only when lineage is true).
- incomplete: true when a step hit its row cap, so the results are partial.

Write EXACTLY two parts separated by a line containing only "###FOLLOWUP###":
1) INTRO: one or two friendly, natural sentences introducing the results —
   mention the payee (if any), the period/context, and what kinds of records are
   shown. Conversational, not a data dump; don't list every number.
2) FOLLOWUP: one short, proactive question offering a sensible next step. Draw
   from `offers`: you can suggest visualising the data as a chart (bar/pie/line),
   downloading the full data as CSV, or exploring a related record type. Pick
   what is most helpful and phrase it warmly, like a colleague. If `charts_shown`
   is true, do NOT offer a chart again.

# Lineage vs. unrelated results — get this right
If `lineage` is TRUE, these records are the TRACED ORIGIN of one another: say so
plainly, e.g. "this payment traces back through N credits to N sales
transactions". Follow the `chain` order when describing it.
If `lineage` is FALSE, the record types were fetched SEPARATELY. Never imply one
came from another, and never write "from the same period", "related to this
payment", or "behind this payment" — describe them as separate result sets.
Never claim records are connected because they share a payee or a period.

If `incomplete` is true, add a brief, honest clause that the results are capped
and may not be the full picture. Do not bury it.

Rules: plain sentences only. No tables, no markdown headings, no bullet lists, no
invented numbers. Keep each part to 1-2 sentences.
"""


def _narrator_context(
    question: str,
    trace: list[dict[str, Any]],
    agent: Agent,
    charts_shown: bool,
) -> dict[str, Any]:
    fact = _fact_steps(trace)
    shown = []
    shown_types: set[str] = set()
    for e in fact:
        spec = catalog().get(e["arguments"]["entity"])
        res = e["result"]
        noun = _entity_noun(spec)
        shown_types.add(noun)
        shown.append({"type": noun, "count": res.get("rowsReturned", 0), "total": res.get("totalValue")})

    payee = None
    for e in trace:
        if (e.get("arguments") or {}).get("entity") == "cs_participant":
            s = e["result"].get("sample") or []
            if s:
                payee = f"{s[0].get('FIRSTNAME','')} {s[0].get('LASTNAME','')}".strip() or None

    related = []
    for name in agent.entities:
        spec = catalog().get(name)
        if spec and _is_fact_entity(spec):
            noun = _entity_noun(spec)
            if noun not in shown_types and noun not in related:
                related.append(noun)

    offers = []
    if not charts_shown:
        offers.append("visualise this as a bar, pie, or line chart")
    offers.append("download the full data as CSV")
    if related:
        offers.append("explore related records: " + ", ".join(related[:4]))

    # Did the plan actually WALK the lineage chain, or just fetch several
    # entities that happen to share a payee/period? Without this the narrator
    # cannot tell the two apart and describes a fan-out as if it were lineage.
    lineage = any(
        _is_trace_entity(catalog().get((e.get("arguments") or {}).get("entity") or ""))
        for e in trace
        if (e.get("result") or {}).get("ok")
        and catalog().get((e.get("arguments") or {}).get("entity") or "")
    )
    incomplete = any((e.get("result") or {}).get("feedTruncated") for e in trace)

    return {
        "question": question,
        "payee": payee,
        "shown": shown,
        "related": related,
        "offers": offers,
        "charts_shown": charts_shown,
        "lineage": lineage,
        "chain": [s["type"] for s in shown] if lineage else [],
        "incomplete": incomplete,
    }


async def _narrate(
    agent: Agent, question: str, trace: list[dict[str, Any]], charts_shown: bool = False
) -> tuple[str, str, int]:
    """Return (intro, followup, tokens); empty strings on any failure."""
    try:
        ctx = _narrator_context(question, trace, agent, charts_shown)
        resp = await chat_completion(
            messages=[
                {"role": "system", "content": _NARRATOR_PROMPT},
                {"role": "user", "content": json.dumps(ctx, default=str)},
            ],
            temperature=0.3,
            max_tokens=220,
        )
        text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        tokens = _usage(resp)
        if "###FOLLOWUP###" in text:
            intro, followup = text.split("###FOLLOWUP###", 1)
            return intro.strip(), followup.strip(), tokens
        return text, "", tokens
    except Exception:  # noqa: BLE001 - narration must never break the answer
        log.exception("narrator failed")
        return "", "", 0


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
    tokens = _usage(plan_resp)

    # No tool call => the planner answered directly (clarification / chat).
    if not tool_calls:
        return AgentRunResult(reply=(msg.get("content") or "").strip(), tool_calls=[], tokens=tokens)

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
            tokens=tokens,
        )

    log.info("planner PLAN=%s", json.dumps(steps, default=str))

    # --- 2. EXECUTE ---
    trace = await _execute_plan(agent, steps)

    # --- 2b. ENRICH (deterministic FK -> label, not the LLM) ---
    enrichment = await _auto_enrich(trace)
    period_names = _period_name_index(trace)

    last_user = next(
        (m["content"] for m in reversed(user_messages) if m.get("role") == "user"),
        "",
    )

    # --- 3. RENDER ---
    # Deterministic table/summary for fact-entity results; the LLM only handles
    # non-tabular answers (positions, single lookups, chit-chat). A narrator adds
    # a friendly intro + proactive follow-up around the deterministic tables.
    datasets: list[dict[str, Any]] = []
    charts: list[dict[str, str]] = []
    body = _try_deterministic_answer(last_user, trace, enrichment, period_names)
    if body is not None:
        datasets = _build_datasets(trace, enrichment, period_names)
        charts = _build_charts(last_user, trace)
        intro, followup, ntok = await _narrate(agent, last_user, trace, charts_shown=bool(charts))
        tokens += ntok
        reply = "\n\n".join(p for p in [intro, body, followup] if p)
    else:
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
            max_tokens=2500,
        )
        tokens += _usage(summary_resp)
        summary_msg = (summary_resp.get("choices") or [{}])[0].get("message", {})
        reply = (summary_msg.get("content") or "").strip()
    log.info("planner REPLY(first 400)=%s", reply[:400])

    # Expose a clean tool trace (drop the internal 'step'/'extracts' noise).
    public_trace = [
        {"name": e["name"], "arguments": e["arguments"], "result": e["result"]}
        for e in trace
    ]
    return AgentRunResult(
        reply=reply,
        tool_calls=public_trace,
        datasets=datasets,
        charts=charts,
        tokens=tokens,
        steps=_build_steps(trace),
    )
