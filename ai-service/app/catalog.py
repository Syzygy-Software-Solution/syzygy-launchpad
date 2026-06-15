"""Entity catalog loader.

Reads every `*.yaml` file under `ai-service/catalog/` at startup and
exposes them as a typed registry. The generic `query_entity` tool reads
this registry to know which entities exist, what filters they accept,
and which OData endpoint to call.

This is the seed of the broader catalog architecture described in
`docs/ai-agent-architecture.md`. Today it powers three entities; tomorrow
adding CS_SALESTRANSACTION is just dropping a new YAML file in.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# Repo layout: ai-service/app/catalog.py → ai-service/catalog/*.yaml
_CATALOG_DIR = Path(__file__).resolve().parent.parent / "catalog"


@dataclass
class FilterSpec:
    key: str                 # caller-facing filter name (lowercase)
    column: str              # OData column name (UPPERCASE)
    op: str                  # eq | ge | le | gt | lt
    type: str                # integer | string | datetime
    description: str = ""


@dataclass
class EntitySpec:
    name: str
    endpoint: str
    description: str
    filters: dict[str, FilterSpec] = field(default_factory=dict)
    require_one_of: list[str] = field(default_factory=list)
    projection: list[str] = field(default_factory=list)
    aggregations: dict[str, dict[str, Any]] = field(default_factory=dict)
    dedupe_by: str | None = None


def _load_entity(path: Path) -> EntitySpec:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    filters_raw = raw.get("filters") or {}
    filters: dict[str, FilterSpec] = {}
    for key, spec in filters_raw.items():
        filters[key] = FilterSpec(
            key=key,
            column=spec["column"],
            op=spec.get("op", "eq"),
            type=spec.get("type", "string"),
            description=spec.get("description", ""),
        )

    return EntitySpec(
        name=raw["name"],
        endpoint=raw["endpoint"],
        description=raw.get("description", ""),
        filters=filters,
        require_one_of=list(raw.get("requireOneOf") or []),
        projection=list(raw.get("projection") or []),
        aggregations=dict(raw.get("aggregations") or {}),
        dedupe_by=raw.get("dedupeBy"),
    )


@lru_cache(maxsize=1)
def catalog() -> dict[str, EntitySpec]:
    """Load every YAML file in the catalog dir, keyed by entity name."""
    if not _CATALOG_DIR.exists():
        log.warning("Catalog dir %s does not exist", _CATALOG_DIR)
        return {}

    registry: dict[str, EntitySpec] = {}
    for path in sorted(_CATALOG_DIR.glob("*.yaml")):
        try:
            spec = _load_entity(path)
            registry[spec.name] = spec
            log.info(
                "catalog: loaded entity '%s' (endpoint=%s, filters=%s)",
                spec.name, spec.endpoint, list(spec.filters),
            )
        except Exception:  # noqa: BLE001 - bad YAML must not crash the app
            log.exception("Failed to load catalog file %s", path)
    return registry


def render_catalog_for_prompt() -> str:
    """Build the catalog block injected into the agent's system prompt.

    Keeping this as plain text (not JSON) keeps token use low and the
    output easy to skim during debugging.
    """
    lines: list[str] = []
    for spec in catalog().values():
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
        lines.append("")  # blank line between entities
    return "\n".join(lines).rstrip()
