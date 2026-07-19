"""Entity catalog loader + join-graph builder.

Reads every enriched entity card under `ai-service/catalog/entities/*.yaml` at
startup and exposes them as a typed registry. This is the SINGLE SOURCE OF
TRUTH for schema knowledge; the generic `query_entity` tool, the query planner,
the schema-RAG index, and the eval suite all read from here.

Card schema (see catalog/entities/cs_payment.yaml for the fully-commented
reference):
  entity/title/version   - identity
  source                 - destination + base-path setting + relative path
  description/grain/...   - semantics used for RAG retrieval + planning
  columns                - authoritative column list; each column declares its
                           own `filterable` operators and (for FKs) `references`
  joins                  - bidirectional graph edges between entities
  query                  - code-enforced guardrails (requireOneOf, top, ...)
  aggregations           - computed by query_entity over the result set
  valueSets/sampleRows   - value hints (filled by the nightly refresh job)

Filters are NOT declared separately — they are DERIVED from each column's
`filterable` block, so "what exists" and "what can be filtered" never drift.
The public `FilterSpec` / `EntitySpec.filters` shape is preserved so the
existing `query_entity` tool keeps working unchanged.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .graph import JoinGraph

log = logging.getLogger(__name__)

# Repo layout: ai-service/app/catalog.py -> ai-service/catalog/entities/*.yaml
_CATALOG_DIR = Path(__file__).resolve().parent.parent / "catalog" / "entities"

# Map card column types -> the filter value types understood by query_entity's
# `_format_value` (integer | string | datetime). Anything unmapped is treated
# as a string literal.
_COLUMN_TYPE_TO_FILTER_TYPE = {
    "bigint": "integer",
    "int": "integer",
    "integer": "integer",
    "datetime": "datetime",
    "timestamp": "datetime",
    "string": "string",
    "decimal": "string",  # decimals are measures, not filtered in practice
}


# ---------------------------------------------------------------------------
# Typed specs
# ---------------------------------------------------------------------------
@dataclass
class FilterSpec:
    key: str                 # caller-facing filter name (lowercase)
    column: str              # OData column name (UPPERCASE)
    op: str                  # eq | ge | le | gt | lt | in
    type: str                # integer | string | datetime
    description: str = ""


@dataclass
class ColumnSpec:
    name: str
    type: str
    role: str = "attribute"       # primary_key | foreign_key | measure | attribute | date
    label: str = ""
    description: str = ""
    synonyms: list[str] = field(default_factory=list)
    cardinality: str | None = None
    references: dict[str, str] | None = None   # {entity, column} for FKs
    aggregatable: list[str] = field(default_factory=list)


@dataclass
class JoinSpec:
    to: str
    via: str                 # local column
    remote_column: str
    relationship: str = "unknown"
    description: str = ""


@dataclass
class EntitySpec:
    # identity
    name: str
    title: str = ""
    version: int = 1
    # source binding
    endpoint: str = ""                     # relativePath (kept for query_entity)
    destination: str = ""
    base_path_setting: str = "tcmp_base_path"
    odata_version: str = "v4"
    # semantics
    description: str = ""
    grain: str = ""
    domains: list[str] = field(default_factory=list)
    synonyms: list[str] = field(default_factory=list)
    # structure
    columns: dict[str, ColumnSpec] = field(default_factory=dict)
    joins: list[JoinSpec] = field(default_factory=list)
    # query contract (derived + declared)
    filters: dict[str, FilterSpec] = field(default_factory=dict)
    require_one_of: list[str] = field(default_factory=list)
    default_top: int = 200
    max_top: int = 1000
    projection: list[str] = field(default_factory=list)
    dedupe_by: str | None = None
    # aggregations + value hints
    aggregations: dict[str, dict[str, Any]] = field(default_factory=dict)
    value_sets: dict[str, dict[str, Any]] = field(default_factory=dict)
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    # deterministic FK-to-label resolution rules (see cs_payment.yaml)
    enrichments: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _filter_type_for(column_type: str) -> str:
    return _COLUMN_TYPE_TO_FILTER_TYPE.get(str(column_type).lower(), "string")


def _load_entity(path: Path) -> EntitySpec:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    source = raw.get("source") or {}
    query = raw.get("query") or {}

    # --- columns + derived filters ---
    columns: dict[str, ColumnSpec] = {}
    filters: dict[str, FilterSpec] = {}
    for col_name, col_raw in (raw.get("columns") or {}).items():
        col_raw = col_raw or {}
        col_type = col_raw.get("type", "string")
        columns[col_name] = ColumnSpec(
            name=col_name,
            type=col_type,
            role=col_raw.get("role", "attribute"),
            label=col_raw.get("label", ""),
            description=col_raw.get("description", "").strip(),
            synonyms=list(col_raw.get("synonyms") or []),
            cardinality=col_raw.get("cardinality"),
            references=col_raw.get("references"),
            aggregatable=list(col_raw.get("aggregatable") or []),
        )
        # Derive one FilterSpec per `filterable` entry on the column.
        for f in col_raw.get("filterable") or []:
            key = f["key"]
            filters[key] = FilterSpec(
                key=key,
                column=col_name,
                op=f.get("op", "eq"),
                type=_filter_type_for(col_type),
                description=columns[col_name].description or columns[col_name].label,
            )

    # --- joins ---
    joins: list[JoinSpec] = []
    for j in raw.get("joins") or []:
        joins.append(
            JoinSpec(
                to=j["to"],
                via=j["via"],
                remote_column=j.get("remoteColumn", j["via"]),
                relationship=j.get("relationship", "unknown"),
                description=j.get("description", "").strip(),
            )
        )

    return EntitySpec(
        name=raw["entity"],
        title=raw.get("title", ""),
        version=int(raw.get("version", 1)),
        endpoint=source.get("relativePath", ""),
        destination=source.get("destination", ""),
        base_path_setting=source.get("basePathSetting", "tcmp_base_path"),
        odata_version=source.get("odataVersion", "v4"),
        description=raw.get("description", "").strip(),
        grain=raw.get("grain", "").strip(),
        domains=list(raw.get("domains") or []),
        synonyms=list(raw.get("synonyms") or []),
        columns=columns,
        joins=joins,
        filters=filters,
        require_one_of=list(query.get("requireOneOf") or []),
        default_top=int(query.get("defaultTop", 200)),
        max_top=int(query.get("maxTop", 1000)),
        projection=list(query.get("projection") or []),
        dedupe_by=query.get("dedupeBy"),
        aggregations=dict(raw.get("aggregations") or {}),
        value_sets=dict(raw.get("valueSets") or {}),
        sample_rows=list(raw.get("sampleRows") or []),
        enrichments=list(raw.get("enrichments") or []),
    )


@lru_cache(maxsize=1)
def catalog() -> dict[str, EntitySpec]:
    """Load every enriched card under catalog/entities/, keyed by entity name."""
    if not _CATALOG_DIR.exists():
        log.warning("Catalog dir %s does not exist", _CATALOG_DIR)
        return {}

    registry: dict[str, EntitySpec] = {}
    for path in sorted(_CATALOG_DIR.glob("*.yaml")):
        try:
            spec = _load_entity(path)
            registry[spec.name] = spec
            log.info(
                "catalog: loaded '%s' (endpoint=%s, filters=%s, joins=%s)",
                spec.name,
                spec.endpoint,
                list(spec.filters),
                [j.to for j in spec.joins],
            )
        except Exception:  # noqa: BLE001 - a bad card must not crash the app
            log.exception("Failed to load catalog file %s", path)
    return registry


@lru_cache(maxsize=1)
def join_graph() -> JoinGraph:
    """Build the join graph from every card's `joins` + column `references`."""
    graph = JoinGraph()
    registry = catalog()

    for spec in registry.values():
        # Ensure every entity is a node even if it has no joins yet.
        graph._adjacency.setdefault(spec.name, [])

        # Edges from explicit `joins:` blocks.
        for j in spec.joins:
            graph.add_edge(
                source=spec.name,
                target=j.to,
                source_column=j.via,
                target_column=j.remote_column,
                relationship=j.relationship,
            )

        # Edges implied by foreign-key `references:` on columns (belt & braces;
        # de-dup in add_edge means declaring both is harmless).
        for col in spec.columns.values():
            if col.role == "foreign_key" and col.references:
                target = col.references.get("entity")
                target_col = col.references.get("column", col.name)
                # Only add if the target entity exists in the catalog.
                if target and target in registry:
                    graph.add_edge(
                        source=spec.name,
                        target=target,
                        source_column=col.name,
                        target_column=target_col,
                        relationship="many-to-one",
                    )

    log.info("join_graph: %d entities, edges=%s", len(graph.entities), graph.entities)
    return graph


# ---------------------------------------------------------------------------
# Prompt rendering (interim — replaced by the planner step)
# ---------------------------------------------------------------------------
def render_catalog_for_prompt(entities: list[str] | None = None) -> str:
    """Build the catalog block injected into the agent's system prompt.

    Keeping this as plain text (not JSON) keeps token use low and the
    output easy to skim during debugging. Optionally scoped to a subset of
    entities (used once RAG selects them).
    """
    registry = catalog()
    names = entities or list(registry)
    lines: list[str] = []
    for name in names:
        spec = registry.get(name)
        if spec is None:
            continue
        lines.append(f"## {spec.name}")
        if spec.description:
            lines.append(spec.description)
        lines.append("Filters:")
        for f in spec.filters.values():
            lines.append(f"  - {f.key} ({f.type}): {f.description}")
        if spec.require_one_of:
            lines.append(
                "At least one of these filter keys is required: "
                + ", ".join(spec.require_one_of)
            )
        if spec.joins:
            lines.append("Joins:")
            for j in spec.joins:
                lines.append(
                    f"  - {j.to} (on {spec.name}.{j.via} = {j.to}.{j.remote_column})"
                )
        lines.append("")  # blank line between entities
    return "\n".join(lines).rstrip()
