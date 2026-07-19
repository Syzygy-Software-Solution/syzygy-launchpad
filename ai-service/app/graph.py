"""Join graph — the direction-agnostic "subway map" of the catalog.

Every entity card declares `joins:` (and per-column `references:`). This module
turns those declarations into an in-memory graph so the query planner can find a
path between ANY two entities in EITHER direction — which is what makes reverse
queries ("all payments by payee X") as reliable as forward ones.

No external resources: this is pure Python, rebuilt from the catalog at startup.

Terminology:
- node  = an entity (e.g. "cs_payment")
- edge  = a join between two entities, labelled with the columns that link them
- path  = an ordered list of edges connecting a start entity to a goal entity
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JoinEdge:
    """A single hop between two entities.

    Always oriented `source` -> `target` for the direction of travel. The graph
    stores each declared join twice (once per direction) so path-finding never
    depends on which card the join was written on.
    """
    source: str          # entity we travel FROM
    target: str          # entity we travel TO
    source_column: str   # column on `source` that holds the join key
    target_column: str   # column on `target` that holds the join key
    relationship: str = "unknown"   # many-to-one | one-to-many | ...

    def describe(self) -> str:
        return (
            f"{self.source}.{self.source_column} -> "
            f"{self.target}.{self.target_column}"
        )


@dataclass
class JoinGraph:
    """Undirected-in-spirit graph stored as a directed adjacency list.

    For every declared join we insert BOTH orientations, so `find_path` can walk
    from A to B regardless of which entity's card declared the edge.
    """
    _adjacency: dict[str, list[JoinEdge]] = field(default_factory=dict)

    # ------------------------------------------------------------------ build
    def add_edge(
        self,
        source: str,
        target: str,
        source_column: str,
        target_column: str,
        relationship: str = "unknown",
    ) -> None:
        forward = JoinEdge(
            source=source,
            target=target,
            source_column=source_column,
            target_column=target_column,
            relationship=relationship,
        )
        reverse = JoinEdge(
            source=target,
            target=source,
            source_column=target_column,
            target_column=source_column,
            relationship=_invert_relationship(relationship),
        )
        self._insert(forward)
        self._insert(reverse)

    def _insert(self, edge: JoinEdge) -> None:
        bucket = self._adjacency.setdefault(edge.source, [])
        # De-dup: the same edge can be declared on both cards.
        if edge not in bucket:
            bucket.append(edge)

    # ------------------------------------------------------------------ query
    @property
    def entities(self) -> list[str]:
        return sorted(self._adjacency)

    def neighbors(self, entity: str) -> list[JoinEdge]:
        return list(self._adjacency.get(entity, []))

    def find_path(self, start: str, goal: str) -> list[JoinEdge] | None:
        """Shortest edge path from `start` to `goal` (BFS), or None.

        Returns an ordered list of `JoinEdge`, each oriented in the direction of
        travel. An empty list means start == goal. `None` means no path exists.
        """
        if start == goal:
            return []
        if start not in self._adjacency or goal not in self._adjacency:
            return None

        # BFS. queue holds (current_entity, path_of_edges_so_far).
        queue: deque[tuple[str, list[JoinEdge]]] = deque([(start, [])])
        visited: set[str] = {start}
        while queue:
            current, path = queue.popleft()
            for edge in self._adjacency.get(current, []):
                if edge.target in visited:
                    continue
                new_path = path + [edge]
                if edge.target == goal:
                    return new_path
                visited.add(edge.target)
                queue.append((edge.target, new_path))
        return None

    def render_for_prompt(self, entities: list[str] | None = None) -> str:
        """Human/LLM-readable edge list, optionally scoped to a subset.

        Used to inject the relevant slice of the graph into the planner prompt.
        Each declared join is printed once (deduped across both orientations).
        """
        scope = set(entities) if entities else None
        seen: set[frozenset[str]] = set()
        lines: list[str] = []
        for source in sorted(self._adjacency):
            if scope is not None and source not in scope:
                continue
            for edge in self._adjacency[source]:
                if scope is not None and edge.target not in scope:
                    continue
                key = frozenset((edge.source, edge.target))
                if key in seen:
                    continue
                seen.add(key)
                lines.append(
                    f"- {edge.source} <-> {edge.target} "
                    f"(join on {edge.source}.{edge.source_column} = "
                    f"{edge.target}.{edge.target_column})"
                )
        return "\n".join(lines)


def _invert_relationship(rel: str) -> str:
    if rel == "many-to-one":
        return "one-to-many"
    if rel == "one-to-many":
        return "many-to-one"
    return rel  # one-to-one / many-to-many / unknown are symmetric
