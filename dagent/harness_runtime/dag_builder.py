"""Compile canonical DAG specs and validate executable graph structure."""

from __future__ import annotations

from collections import defaultdict, deque
import re
from typing import Any
from uuid import uuid4

from dagent.harness_runtime.artifacts import validate_artifact_paths
from dagent.schemas.dag import iter_dag_invocations
from dagent.schemas import (
    DAG,
    DAGEdge,
    DAGNode,
    DAGSpec,
    CapabilityNodePayload,
    CapabilityDefinition,
    CapabilityInvocation,
    LoopNodePayload,
    MapNodePayload,
    StartNodePayload,
    SubgraphNodePayload,
)
from dagent.schemas.value import (
    ItemExpr,
    NodeOutputExpr,
    ValueExpressionError,
    iter_artifact_exprs,
    iter_value_exprs,
)


class DAGCreationError(ValueError):
    """Raised when a proposed DAG cannot become an executable DAG."""


class DAGValidationError(ValueError):
    """Raised when a DAG violates structural validation rules."""


def compile_dag_spec(
    spec: DAGSpec,
    *,
    task_id: str,
    capabilities: list[CapabilityDefinition] | None = None,
) -> DAG:
    validate_dag_spec(spec)
    definitions_by_id = (
        None
        if capabilities is None
        else {
            definition.id: definition
            for definition in capabilities
        }
    )
    nodes = [
        _normalize_dag_spec_node(node, definitions_by_id)
        for node in spec.nodes
    ]
    edges = [edge.model_copy(deep=True) for edge in spec.edges]
    return DAG(
        dag_id=f"dag_{uuid4().hex}",
        task_id=task_id,
        status="draft",
        nodes=nodes,
        edges=edges,
    )


def validate_dag_spec(spec: DAGSpec) -> None:
    for artifact_id, artifact in spec.artifacts.items():
        if artifact.id != artifact_id:
            raise DAGValidationError(
                f"Artifact key '{artifact_id}' must match artifact id '{artifact.id}'."
            )
        try:
            validate_artifact_paths(artifact.paths)
        except ValueError as exc:
            raise DAGValidationError(str(exc)) from exc

    artifact_ids = set(spec.artifacts)
    for node in spec.nodes:
        references = [*node.inputs, *node.outputs]
        unknown = sorted(set(references) - artifact_ids)
        if unknown:
            joined = ", ".join(unknown)
            raise DAGValidationError(
                f"Node '{node.id}' references unknown artifact(s): {joined}."
            )

    _validate_dag_spec_value_expressions(spec)
    _validate_dag_spec_output(spec)
    _validate_artifact_data_dependencies(spec)

    validate_dag(
        DAG(
            dag_id=f"dag_spec_{spec.id}",
            task_id=spec.id,
            nodes=[node.model_copy(deep=True) for node in spec.nodes],
            edges=[edge.model_copy(deep=True) for edge in spec.edges],
        )
    )

    for node in spec.nodes:
        embedded = _embedded_spec(node.payload)
        if embedded is None:
            continue
        try:
            validate_dag_spec(embedded)
        except DAGValidationError as exc:
            raise DAGValidationError(f"Node '{node.id}' embedded DAG: {exc}") from exc


def _embedded_spec(payload: Any) -> DAGSpec | None:
    if isinstance(payload, SubgraphNodePayload):
        return payload.spec
    if isinstance(payload, LoopNodePayload):
        return payload.body
    return None


def _payload_value_sources(payload: Any) -> list[tuple[Any, bool]]:
    """Return ``(value, item_allowed)`` pairs of expression-bearing payload fields."""
    if isinstance(payload, CapabilityNodePayload):
        invocation = payload.invocation
        return [
            (invocation.arguments, False),
            (invocation.boundary.allowed_paths, False),
        ]
    if isinstance(payload, MapNodePayload):
        invocation = payload.invocation
        return [
            (payload.items, False),
            (invocation.arguments, True),
            (invocation.boundary.allowed_paths, True),
        ]
    if isinstance(payload, SubgraphNodePayload):
        return [(payload.input, False)]
    if isinstance(payload, LoopNodePayload):
        return [(payload.input, False), (payload.until, True)]
    return []


def _validate_dag_spec_value_expressions(spec: DAGSpec) -> None:
    artifact_ids = set(spec.artifacts)

    def check_artifact_refs(value: Any, owner: str) -> None:
        try:
            artifact_refs = list(iter_artifact_exprs(value))
        except ValueExpressionError as exc:
            raise DAGValidationError(f"{owner} has invalid value expression: {exc}") from exc
        for ref in artifact_refs:
            if ref.artifact_id not in artifact_ids:
                raise DAGValidationError(
                    f"{owner} references unknown artifact '{ref.artifact_id}' in value expression."
                )

    for node in spec.nodes:
        for value, _ in _payload_value_sources(node.payload):
            check_artifact_refs(value, f"Node '{node.id}'")
    for edge in spec.edges:
        if edge.when is not None:
            check_artifact_refs(edge.when, f"Edge '{edge.source}->{edge.target}' condition")
    if spec.output is not None:
        check_artifact_refs(spec.output, "DAG output")


def _validate_dag_spec_output(spec: DAGSpec) -> None:
    if spec.output is None:
        return
    node_ids = {node.id for node in spec.nodes}
    try:
        exprs = list(iter_value_exprs(spec.output))
    except ValueExpressionError as exc:
        raise DAGValidationError(f"DAG output has invalid value expression: {exc}") from exc
    for expr in exprs:
        if isinstance(expr, ItemExpr):
            raise DAGValidationError("DAG output cannot use item expressions.")
        if isinstance(expr, NodeOutputExpr) and expr.node_id not in node_ids:
            raise DAGValidationError(
                f"DAG output reads from unknown node '{expr.node_id}'."
            )


def _validate_artifact_data_dependencies(spec: DAGSpec) -> None:
    producers: dict[str, list[str]] = defaultdict(list)
    for node in spec.nodes:
        for artifact_id in set(node.outputs):
            producers[artifact_id].append(node.id)

    for artifact_id, producer_ids in sorted(producers.items()):
        unique_producers = sorted(set(producer_ids))
        if len(unique_producers) > 1:
            joined = ", ".join(unique_producers)
            raise DAGValidationError(
                f"Artifact '{artifact_id}' is produced by multiple nodes: {joined}."
            )

    incoming = _incoming_edges(spec.edges)

    upstream_cache: dict[str, set[str]] = {}

    def upstream_ids(node_id: str) -> set[str]:
        if node_id in upstream_cache:
            return upstream_cache[node_id]
        upstream_cache[node_id] = _upstream_ids(node_id, incoming)
        return upstream_cache[node_id]

    producer_by_artifact = {
        artifact_id: producer_ids[0]
        for artifact_id, producer_ids in producers.items()
    }
    for node in spec.nodes:
        upstream = upstream_ids(node.id)
        for artifact_id in set(node.inputs):
            producer_id = producer_by_artifact.get(artifact_id)
            if producer_id is None or producer_id == node.id:
                continue
            if producer_id not in upstream:
                raise DAGValidationError(
                    f"Node '{node.id}' reads artifact '{artifact_id}' and must depend "
                    f"on producer node '{producer_id}'."
                )


def _incoming_edges(edges: list[DAGEdge]) -> dict[str, list[str]]:
    incoming: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        incoming[edge.target].append(edge.source)
    return incoming


def _upstream_ids(node_id: str, incoming: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(incoming.get(node_id, ()))
    while stack:
        source = stack.pop()
        if source in seen:
            continue
        seen.add(source)
        stack.extend(incoming.get(source, ()))
    return seen


def _normalize_dag_spec_node(
    node: DAGNode,
    definitions_by_id: dict[str, CapabilityDefinition] | None,
) -> DAGNode:
    normalized = node.model_copy(deep=True)
    if definitions_by_id is None:
        return normalized
    for invocation in iter_dag_invocations([normalized]):
        if not invocation.capability_id:
            continue
        definition = definitions_by_id.get(invocation.capability_id)
        if definition is None:
            available = ", ".join(sorted(definitions_by_id)) or "(none)"
            raise DAGValidationError(
                f"Unknown capability '{invocation.capability_id}'. "
                f"Available capabilities: {available}."
            )
        invocation.kind = definition.kind
        invocation.risk = definition.policy.risk
    return normalized


def validate_dag(dag: DAG) -> None:
    """Validate DAG structure."""
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

    for node in dag.nodes:
        if isinstance(node.payload, StartNodePayload):
            if node.id != "start":
                raise DAGValidationError("Start node must use id 'start'.")
            continue
        if (
            isinstance(node.payload, (CapabilityNodePayload, MapNodePayload))
            and not node.payload.invocation.capability_id
        ):
            raise DAGValidationError(f"Node '{node.id}' must declare a capability.")

    node_id_set = set(node_ids)
    connected_ids: set[str] = set()
    for edge in dag.edges:
        if edge.source not in node_id_set:
            raise DAGValidationError(
                f"Edge source '{edge.source}' does not reference an existing node."
            )
        if edge.target not in node_id_set:
            raise DAGValidationError(
                f"Edge target '{edge.target}' does not reference an existing node."
            )
        connected_ids.add(edge.source)
        connected_ids.add(edge.target)

    _validate_value_expression_dependencies(dag.nodes, dag.edges)

    if len(node_id_set) > 1:
        isolated_ids = node_id_set - connected_ids
        if isolated_ids:
            isolated_list = ", ".join(sorted(isolated_ids))
            raise DAGValidationError(f"Isolated node IDs: {isolated_list}.")

    _ensure_acyclic(node_id_set, [(edge.source, edge.target) for edge in dag.edges])


def _validate_value_expression_dependencies(nodes: list[DAGNode], edges: list[DAGEdge]) -> None:
    incoming = _incoming_edges(edges)
    upstream_cache: dict[str, set[str]] = {}
    node_ids = {node.id for node in nodes}

    def upstream_ids(node_id: str) -> set[str]:
        if node_id not in upstream_cache:
            upstream_cache[node_id] = _upstream_ids(node_id, incoming)
        return upstream_cache[node_id]

    def check_exprs(value: Any, *, owner: str, reader_id: str, item_allowed: bool) -> None:
        try:
            exprs = list(iter_value_exprs(value))
        except ValueExpressionError as exc:
            raise DAGValidationError(f"{owner} has invalid value expression: {exc}") from exc
        upstream = upstream_ids(reader_id)
        for expr in exprs:
            if isinstance(expr, ItemExpr) and not item_allowed:
                raise DAGValidationError(
                    f"{owner} uses an item expression outside a map body or loop condition."
                )
            if isinstance(expr, NodeOutputExpr):
                if expr.node_id not in node_ids:
                    raise DAGValidationError(
                        f"{owner} reads output from unknown node '{expr.node_id}'."
                    )
                if expr.node_id not in upstream:
                    raise DAGValidationError(
                        f"{owner} reads output from node '{expr.node_id}' and must depend on it."
                    )

    for node in nodes:
        for value, item_allowed in _payload_value_sources(node.payload):
            check_exprs(value, owner=f"Node '{node.id}'", reader_id=node.id, item_allowed=item_allowed)

    for edge in edges:
        if edge.when is None:
            continue
        check_exprs(
            edge.when,
            owner=f"Edge '{edge.source}->{edge.target}' condition",
            reader_id=edge.target,
            item_allowed=False,
        )


MAX_EXECUTION_CONTEXT_CHARS = 16000


def context_excerpt(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[TRUNCATED after {limit} chars]"


def strip_thinking_blocks(content: str) -> str:
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"<think>.*", "", content, flags=re.IGNORECASE | re.DOTALL)


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
