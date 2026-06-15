# Production-Grade Architecture for the Multi-Agent AI System

> Scope: SAP Incentive Management (Commissions) data, accessed via SAP
> Datasphere consumption APIs (`TCMP_DEST`). Today: one agent, one tool,
> three filters on `CS_PAYMENT`. Target: dozens of tables, multi-hop
> queries, customer-specific values, zero-prompt-edit when schemas change.

---

## Part A — Scaling beyond a single hand-crafted tool

### 1. Schema registry, not prompt-baked column knowledge
Stop teaching the LLM about columns inside the system prompt. Build a
**catalog** — structured store (YAML / JSON / DB / CSV) that describes
every entity once. Example:

```yaml
# catalog/entities/cs_payment.yaml
entity: CS_PAYMENT
odataSet: C_V_PAYMENT
destination: TCMP_DEST
basePathSetting: tcmp_base_path
relativePath: /C_V_PAYMENT/C_V_PAYMENT
description: One row per payee payment per period.
joins:
  - to: CS_PERIODS
    on: PERIODSEQ
  - to: CS_PAYEE
    on: PAYEESEQ
filters:
  required_one_of: [PERIODSEQ, EARNINGGROUPID, EARNINGCODEID]
  recommended:     [PERIODSEQ]      # because table is huge
columns:
  PERIODSEQ:
    type: bigint
    description: FK to CS_PERIODS.PERIODSEQ
    user_friendly_synonyms: [period, pay period, period sequence]
  EARNINGGROUPID:
    type: string
    description: High-level category (Commission, Bonus, ...)
  EARNINGCODEID:
    type: string
    description: Specific earning code within a group.
```

The catalog is the **single source of truth**. Tool specs, prompt
snippets, the URL builder, and the eval suite all read from it.
Renaming a column or adding a table = edit one YAML file, no prompt
surgery, no agent redeploy.

### 2. RAG over the schema — load only relevant entities per query
Don't put 30 table descriptions into every prompt. Use the
**embeddings model already provisioned in AI Core** to embed each
entity's description + column docs. Store vectors in HANA Cloud Vector
Engine (or Postgres pgvector — both work on BTP).

At inference time:
```
user query -> embed -> top-K entities -> only those cards go into the prompt
```
Typical K = 3–5. System prompt becomes:
> "Here are the entities relevant to this question: {{loaded_cards}}.
>  Plan a query that answers it."

Same pattern used by Microsoft's NL2SQL, LangChain SQL Agent, SAP Joule.

### 3. One generic `query_entity` tool, not one tool per table
Replace `query_payments` with:
```
query_entity(entity: str, filters: dict, top: int, select: list[str])
```
Runtime validation happens in Python against the catalog:
- entity must exist
- filters must satisfy `required_one_of`
- `select` columns must exist
- if no `recommended` filter present, return `{ok: false, error: "missing_recommended_filter"}`
  so the LLM asks the user

For the LLM you advertise this **one** tool plus the loaded entity cards.
One tool definition, N entities — no prompt growth.

### 4. Multi-hop questions: planner → executor

**Example user query:** *"Get me the payments for last 2 months and who
are the payees."*

Required hops:
1. `CS_PERIODS` → resolve "last 2 months" → list of `PERIODSEQ`
2. `CS_PAYMENT` → filter by those `PERIODSEQ` → rows + `PAYEESEQ` list
3. `CS_PAYEE` → resolve `PAYEESEQ` → human names

Two viable patterns:

**Option A — ReAct loop with the generic tool (simplest)**
Trust the LLM to plan multi-step. With the generic tool + catalog
cards containing `joins`, it will chain calls naturally. Bump
`max_tool_iterations` from 3 to ~8.
- Use **gpt-4o** (not mini) for the planning step — the difference
  between ~60% and ~95% reliability on multi-hop.
- Keep mini for resolvers / formatting / single-table summarisation.

**Option B — Explicit planner agent (more deterministic)**
Add a `plan_query(user_question)` step that emits a typed plan; a
deterministic executor runs each step. Slower to build but predictable
for demos and unit tests.

> Start with A. Move to B only when you have evidence the LLM is
> misplanning often.

### 5. Filter discipline — enforce in code, hint in catalog
Fear of "millions of rows with no filter" is real. Put the safety net
in code:
- Catalog declares `required_one_of` and `max_rows`.
- Generic tool refuses any call that violates these →
  `{ok: false, error: "missing_required_filter", hint: "..."}`.
- LLM sees the error and asks the user.

Much more reliable than asking the prompt to police it.

### 6. Endpoint / route changes — catalog owns the URL
Make the catalog responsible for URL fragments. When Datasphere changes
a path, edit one YAML line — no Python change, no agent redeploy.

Better: write `scripts/discover_datasphere.py` that hits the OData
`$metadata` endpoint, parses the EDMX XML, and regenerates catalog
skeletons automatically. Schedule it (CI cron) to detect schema drift
**before** users do.

### 7. Identifier resolution — a class of tools, not a table
Users say "payee John Smith", not `PAYEESEQ=4503599627370598`. Build
small resolver tools the LLM can call:
- `resolve_payee(name_or_email)` → `[{PAYEESEQ, displayName}]`
- `resolve_period(month, year)`  → `[{PERIODSEQ, startDate, endDate}]`
- `resolve_earningcode(text)`    → fuzzy match against CS_EARNINGCODES

Each is ~30 LOC of Python. The LLM learns to chain
`resolve_period → query_entity(CS_PAYMENT, periodseq=...)`. This is how
Joule and similar products handle the "user doesn't know the surrogate
key" problem.

### 8. Eval suite — non-negotiable at this scale
With 30 tables you cannot tune the prompt by clicking through the UI.
Set up `pytest` with 20–50 canonical `question → expected-tool-call`
test cases. Run on every prompt/catalog change. Catches "agent now
confuses code with group" before it hits the demo.

### 9. Observability
Log every tool call with chosen `entity`, `filters`, `rowsReturned`. You
already log `query_payments` — generalise it. After a week of real
traffic you'll know exactly where to invest catalog enrichment.

---

## Part B — Value-to-column mapping (e.g. "SPIFF" → which column?)

The catalog tells the LLM **"this column exists"**. It does NOT tell it
**"the string SPIFF is a value of that column"**. That's a separate
problem with a separate solution. Four techniques, combined in
production.

### 1. Value catalogs (inline distinct values for low-cardinality cols)
For columns with bounded cardinality (typically < ~10,000 distinct
values): `EARNINGCODEID`, `EARNINGGROUPID`, `CREDITTYPEID`,
`BUSINESSUNIT`, `STATUS`, ...
**Materialise the distinct values into the catalog YAML.**

```yaml
columns:
  EARNINGGROUPID:
    type: string
    cardinality: low
    sampleValuesUpdated: 2026-06-09
    values: [Commission, Bonus, Adjustment]
  EARNINGCODEID:
    type: string
    cardinality: low
    values: [MBO, SPIFF, "Named Account", QBR, ...]
```

Populated by a nightly job:
```sql
SELECT DISTINCT EARNINGCODEID FROM C_V_PAYMENT
```

Now when the user says "SPIFF", the LLM has SPIFF in its context as a
known value of `EARNINGCODEID` and routes to the correct filter
automatically. One catalog refresh, zero prompt edits, **customer-
specific values supported out of the box**.

> This single technique solves ~70% of the value-mapping problem.

### 2. Value-to-column reverse index (for high-cardinality values)
When the value set is too big to inline (say 50,000 product codes),
don't put values in the catalog — put them in a **reverse index** keyed
by value:

```
index["SPIFF"] -> [{entity: CS_PAYMENT,      column: EARNINGCODEID, popularity: 0.8},
                   {entity: CS_EARNINGCODES, column: CODE,          popularity: 1.0}]
index["MBO"]   -> [{entity: CS_PAYMENT,      column: EARNINGCODEID, popularity: 0.6}]
index["Bonus"] -> [{entity: CS_PAYMENT,      column: EARNINGGROUPID, popularity: 0.9}]
```

Build once (nightly job, same data as #1). Storage: HANA, Postgres,
SQLite FTS5 — whichever is convenient.

Add a tool the LLM can call:
```
disambiguate_value(text)
```
- Returns ranked list of `{entity, column, confidence}` candidates.
- LLM picks based on context, or asks the user when ambiguous.

This is how Snowflake Cortex, Databricks Genie, and SAP Joule handle
the same problem internally.

### 3. Embeddings for fuzzy / synonym / typo matches
Techniques #1 and #2 are exact-match. For fuzzy cases — "named
accounts" vs `Named Account`, "year-end bonus" vs `Bonus`, typos:
- Embed every distinct value once with `text-embedding-3-small`
  (already provisioned).
- Store in HANA Cloud Vector Engine.
- On miss, do a vector lookup with cosine threshold ≥ 0.8.

`disambiguate_value` becomes: exact-match index first, vector fallback.

### 4. Sample rows in the entity card (highest ROI today)
When loading an entity card via RAG, include **5–10 actual sample rows**
at the bottom:

```yaml
sampleRows:
  - {PAYMENTSEQ: 26177..., EARNINGGROUPID: Commission, EARNINGCODEID: MBO,   VALUE: 175.76}
  - {PAYMENTSEQ: 26177..., EARNINGGROUPID: Bonus,      EARNINGCODEID: SPIFF, VALUE: 211.40}
```

The LLM sees the **actual shape** of data — including that
Commission/Bonus live in `EARNINGGROUPID` and MBO/SPIFF live in
`EARNINGCODEID`. Far more reliable than describing it in prose.

> Refresh sample rows nightly (same job as #1).
> This is the single highest-ROI change you can make right now.

---

## Putting it together — runtime flow

### Simple case: *"Get me the payments related to SPIFF"*
```
1. User: "Get me the payments related to SPIFF"

2. RAG retrieves top-3 entity cards. CS_PAYMENT card includes:
   - column list with cardinality info
   - distinct values inline:
       EARNINGGROUPID -> [Commission, Bonus, ...]
       EARNINGCODEID  -> [MBO, SPIFF, "Named Account", ...]
   - 8 sample rows

3. LLM sees "SPIFF" appears in the EARNINGCODEID value list in the card.
   -> calls query_entity(entity="CS_PAYMENT",
                          filters={EARNINGCODEID: "SPIFF"})

4. Tool runs, returns rows.

5. LLM summarises in plain prose.
```
The LLM never guessed. The catalog brought the value-to-column mapping
into the prompt with the question.

### High-cardinality case: *"Payments for Acme Corp"*
```
1. User mentions "Acme Corp".
2. LLM calls disambiguate_value("Acme Corp").
3. Tool returns:
     [{entity: CS_PARTNER, column: PARTNERNAME, exact_match: false,
       candidates: [{PARTNERSEQ: 9991, PARTNERNAME: "Acme Corporation"}]}]
4. LLM calls query_entity(CS_PAYMENT, filters={PARTNERSEQ: 9991}).
5. Summarise.
```

### Multi-hop case: *"Payments for last 2 months and who are the payees"*
```
1. resolve_period("last 2 months") -> [PERIODSEQ_a, PERIODSEQ_b]
2. query_entity(CS_PAYMENT, filters={PERIODSEQ_in: [a, b]})
   -> rows + distinct PAYEESEQ list
3. query_entity(CS_PAYEE, filters={PAYEESEQ_in: [...]})
   -> human names
4. Join in the formatter step, summarise.
```

---

## Concrete next-step roadmap for this codebase

In ROI order:

1. **Extract `payments_api.py` → `catalog/cs_payment.yaml` + generic
   `tools/query_entity.py`.** Behaviour identical, but adding
   `CS_PERIODS` becomes a 30-line YAML, not a new Python module.

2. **Add `resolve_period(month_name, year)` resolver tool.** Single
   biggest UX win — users say "October 2024" instead of a 16-digit id.

3. **Nightly catalog-refresh script (`scripts/refresh_catalog.py`)** that:
   - `SELECT DISTINCT col FROM tbl LIMIT 10000` for low-cardinality cols
     flagged in YAML → populates `values:` block.
   - `SELECT * FROM tbl SAMPLE 10` → populates `sampleRows:`.
   - Writes the YAML back in place.

4. **Build the value-to-column reverse index**
   (`catalog/value_index.json` initially; HANA table later).

5. **Embed entity cards** with `text-embedding-3-small` (your
   `genai_embed_deployment_id` is wired up). Store vectors in HANA Cloud
   Vector Engine. Add a retrieval step before each LLM call.

6. **Add `disambiguate_value(text)` tool** (exact-match index + vector
   fallback).

7. **`scripts/discover_datasphere.py`** — pull `$metadata`, emit catalog
   skeletons automatically. Manual today; CI cron later.

8. **`tests/test_agent_calls.py`** — ~20 fixed scenarios using a mocked
   LLM client; checks tool name, entity, filters.

9. **Switch the planning model to `gpt-4o`** once 3+ hop queries are in
   scope. Keep `gpt-4o-mini` for resolvers and formatting.

10. **Per-column inlining cap** (e.g. `inlineValuesUpTo: 200` in YAML).
    Above the threshold, force the LLM to call `disambiguate_value` —
    keeps prompts bounded as the catalog grows.

---

## TL;DR

- **Stop teaching the LLM about your data inside the prompt.** Teach it
  a small, generic interface to a metadata catalog that owns all data
  knowledge.
- The catalog is the single source of truth for schemas, joins, filter
  rules, URLs, distinct values, and sample rows.
- Generic `query_entity` tool + RAG-loaded entity cards is the only
  shape that scales beyond ~5 tables.
- Multi-hop = ReAct loop on a planner LLM (gpt-4o) + small resolver
  tools for surrogate-key lookups.
- Value-to-column mapping (the "SPIFF" problem) is solved by inlining
  distinct values for low-cardinality columns, a reverse index +
  embeddings for high-cardinality, and **sample rows in every card**.
- Refresh catalog nightly. Validate prompts/changes with a pytest eval
  suite. Log every tool call.
