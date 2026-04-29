"""DAG validation helpers for planner and executor boundaries."""

from __future__ import annotations

from collections import defaultdict, deque

from dagent.schemas import DAG


class DAGValidationError(ValueError):
    """Raised when a DAG violates structural validation rules."""


def validate_dag(dag: DAG) -> None:
    """Validate DAG structure.

    Checks:
    - at least one node
    - node IDs are unique
    - edges reference existing nodes
    - graph is acyclic
    """
    if not dag.nodes:
        raise DAGValidationError("DAG must contain at least one node.")

    node_ids = [node.id for node in dag.nodes]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for node_id in node_ids:
        if node_id in seen:
            duplicates.add(node_id)
        seen.add(node_id)
    if duplicates:
        duplicate_list = ", ".join(sorted(duplicates))
        raise DAGValidationError(f"Duplicate node IDs: {duplicate_list}.")

    node_id_set = set(node_ids)
    for edge in dag.edges:
        if edge.source not in node_id_set:
            raise DAGValidationError(
                f"Edge source '{edge.source}' does not reference an existing node."
            )
        if edge.target not in node_id_set:
            raise DAGValidationError(
                f"Edge target '{edge.target}' does not reference an existing node."
            )

    _ensure_acyclic(node_id_set, [(edge.source, edge.target) for edge in dag.edges])


def _ensure_acyclic(node_ids: set[str], edges: list[tuple[str, str]]) -> None:
    outgoing: dict[str, list[str]] = defaultdict(list)
    indegree = {node_id: 0 for node_id in node_ids}

    for source, target in edges:
        outgoing[source].append(target)
        indegree[target] += 1

    queue = deque(node_id for node_id, degree in indegree.items() if degree == 0)
    visited_count = 0

    while queue:
        current = queue.popleft()
        visited_count += 1
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)

    if visited_count != len(node_ids):
        raise DAGValidationError("DAG must be acyclic.")
