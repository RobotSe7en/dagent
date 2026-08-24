"""Compile canonical DAG specs and validate executable graph structure."""

from __future__ import annotations

from collections import defaultdict, deque
import json
import re
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, best_match
from pydantic import BaseModel
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from dagent.harness_runtime.artifacts import validate_artifact_paths
from dagent.schemas.dag import iter_dag_invocations
from dagent.schemas import (
    DAG,
    DAGEdge,
    DAGNode,
    DAGSpec,
    DAGDiagnostic,
    CapabilityNodePayload,
    ConditionNodePayload,
    CapabilityDefinition,
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


class DAGInputValidationError(ValueError):
    """Raised when graph input does not satisfy a DAG's declared input schema."""

    def __init__(
        self,
        message: str,
        *,
        path: tuple[str | int, ...] = (),
        schema_path: tuple[str | int, ...] = (),
    ) -> None:
        super().__init__(message)
        self.path = path
        self.schema_path = schema_path


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
    _dag_input_validator(spec.input_schema)

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


def inspect_dag_spec(spec: DAGSpec) -> tuple[DAGDiagnostic, ...]:
    """Return deterministic diagnostics without changing validation exceptions.

    ``validate_dag_spec`` remains the authoritative compatibility boundary. This
    inspection facade reports its failure as structured data while preserving
    the validator's existing fail-fast message and exception behavior.
    """

    if not isinstance(spec, DAGSpec):
        raise TypeError("inspect_dag_spec expects a DAGSpec.")
    try:
        validate_dag_spec(spec)
    except DAGValidationError as exc:
        message = str(exc)
        node_id = _diagnostic_node_id(message)
        return (DAGDiagnostic(
            severity="error",
            code=_diagnostic_code(message),
            message=message,
            node_id=node_id,
            path=_diagnostic_path(message, node_id=node_id),
        ),)
    return ()


def _diagnostic_node_id(message: str) -> str | None:
    match = re.search(r"\bNode '([^']+)'", message)
    return None if match is None else match.group(1)


def _diagnostic_code(message: str) -> str:
    lowered = message.lower()
    if "input_schema" in lowered or "input schema" in lowered:
        return "dag.input_schema.invalid"
    if "dag output" in lowered:
        return "dag.output.invalid"
    if "artifact" in lowered:
        return "dag.artifact.invalid"
    if "edge" in lowered or "branch" in lowered:
        return "dag.edge.invalid"
    if "node" in lowered:
        return "dag.node.invalid"
    return "dag.structure.invalid"


def _diagnostic_path(
    message: str,
    *,
    node_id: str | None,
) -> tuple[str | int, ...]:
    lowered = message.lower()
    if node_id is not None:
        return ("nodes", node_id)
    artifact_match = re.search(r"\bArtifact '([^']+)'", message)
    if artifact_match is not None:
        return ("artifacts", artifact_match.group(1))
    if "input_schema" in lowered or "input schema" in lowered:
        return ("input_schema",)
    if "dag output" in lowered:
        return ("output",)
    return ()


def validate_dag_input(
    spec_or_schema: DAGSpec | dict[str, Any],
    graph_input: Any,
) -> None:
    """Validate graph input without coercion, mutation, or applying schema defaults."""

    schema = (
        spec_or_schema.input_schema
        if isinstance(spec_or_schema, DAGSpec)
        else spec_or_schema
    )
    if not isinstance(schema, dict):
        raise TypeError("spec_or_schema must be a DAGSpec or JSON Schema object.")
    validator = _dag_input_validator(schema)
    instance = (
        graph_input.model_dump(mode="json", by_alias=True)
        if isinstance(graph_input, BaseModel)
        else graph_input
    )
    error = best_match(validator.iter_errors(instance))
    if error is None:
        return
    path = tuple(error.absolute_path)
    schema_path = tuple(error.absolute_schema_path)
    raise DAGInputValidationError(
        f"Graph input does not match input_schema at {error.json_path}: {error.message}",
        path=path,
        schema_path=schema_path,
    ) from error


def _dag_input_validator(schema: dict[str, Any]) -> Draft202012Validator:
    try:
        json.dumps(schema, allow_nan=False)
        Draft202012Validator.check_schema(schema)
        resource = DRAFT202012.create_resource(schema)
        root_uri = DRAFT202012.id_of(schema) or "urn:dagent:dag-input-schema"
        registry = Registry().with_resource(root_uri, resource).crawl()
        _validate_schema_references(
            schema,
            resolver=registry.resolver(root_uri),
        )
    except (TypeError, ValueError) as exc:
        raise DAGValidationError(
            "DAG input_schema is not a valid JSON document: "
            f"{exc}."
        ) from exc
    except SchemaError as exc:
        raise DAGValidationError(
            "DAG input_schema is not a valid JSON Schema Draft 2020-12 "
            f"document: {exc.message}"
        ) from exc
    except Unresolvable as exc:
        raise DAGValidationError(
            "DAG input_schema is not self-contained: "
            f"reference '{exc.ref}' cannot be resolved."
        ) from exc
    return Draft202012Validator(schema, registry=registry)


def _validate_schema_references(schema: Any, *, resolver: Any) -> None:
    if isinstance(schema, dict):
        for keyword in ("$ref", "$dynamicRef"):
            reference = schema.get(keyword)
            if isinstance(reference, str):
                resolver.lookup(reference)
    for subresource_schema in DRAFT202012.subresources_of(schema):
        subresource = DRAFT202012.create_resource(subresource_schema)
        _validate_schema_references(
            subresource_schema,
            resolver=resolver.in_subresource(subresource),
        )


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
    if isinstance(payload, ConditionNodePayload):
        return [(case.when, False) for case in payload.cases]
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
    nodes_by_id = {node.id: node for node in dag.nodes}
    condition_branches: dict[str, set[str]] = {}
    for node in dag.nodes:
        if not isinstance(node.payload, ConditionNodePayload):
            continue
        branches = [case.branch for case in node.payload.cases]
        if any(not branch.strip() for branch in branches):
            raise DAGValidationError(
                f"Condition node '{node.id}' case branches must be non-empty."
            )
        duplicates = sorted({branch for branch in branches if branches.count(branch) > 1})
        if duplicates:
            raise DAGValidationError(
                f"Condition node '{node.id}' has duplicate case branches: "
                + ", ".join(duplicates)
                + "."
            )
        default_branch = node.payload.default_branch
        if not default_branch.strip():
            raise DAGValidationError(
                f"Condition node '{node.id}' default_branch must be non-empty."
            )
        if default_branch in branches:
            raise DAGValidationError(
                f"Condition node '{node.id}' default branch '{default_branch}' "
                "duplicates a case branch."
            )
        condition_branches[node.id] = {*branches, default_branch}

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
        if edge.when is not None and edge.branch is not None:
            raise DAGValidationError(
                f"Edge '{edge.source}->{edge.target}' cannot declare both when and branch."
            )
        source_payload = nodes_by_id[edge.source].payload
        if isinstance(source_payload, ConditionNodePayload):
            if edge.branch is None:
                raise DAGValidationError(
                    f"Condition node '{edge.source}' outgoing edge to '{edge.target}' "
                    "must declare a branch."
                )
            if edge.branch not in condition_branches[edge.source]:
                raise DAGValidationError(
                    f"Condition node '{edge.source}' outgoing edge to '{edge.target}' "
                    f"references unknown branch '{edge.branch}'."
                )
        elif edge.branch is not None:
            raise DAGValidationError(
                f"Edge '{edge.source}->{edge.target}' declares branch '{edge.branch}', "
                "but its source is not a condition node."
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
