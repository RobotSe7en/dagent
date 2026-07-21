"""Normalize typed dynamic planner output into canonical DAGSpec objects."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from jsonschema import Draft202012Validator, validators

from dagent.capabilities.boundaries import infer_capability_boundary
from dagent.dag_builder import Dag
from dagent.harness_runtime.dag_builder import (
    DAGCreationError,
    DAGValidationError,
    validate_dag_spec,
)
from dagent.harness_runtime.planner_schema import (
    PlannerArtifactValue,
    PlannerCapabilityNode,
    PlannerCompareValue,
    PlannerFormatValue,
    PlannerGraph,
    PlannerGraphInputValue,
    PlannerItemValue,
    PlannerListValue,
    PlannerLiteralValue,
    PlannerMapNode,
    PlannerNamedValue,
    PlannerNodeOutputValue,
    PlannerObjectValue,
    PlannerSubgraphNode,
    PlannerLoopNode,
    PlannerValue,
)
from dagent.schemas import (
    Artifact,
    CapabilityDefinition,
    CapabilityInvocation,
    CapabilityNodePayload,
    DAGEdge,
    DAGNode,
    DAGSpec,
    LoopNodePayload,
    MapNodePayload,
    StartNodePayload,
    SubgraphNodePayload,
)
from dagent.schemas.value import (
    ArtifactExpr,
    CompareExpr,
    FormatExpr,
    GraphInputExpr,
    ItemExpr,
    NodeOutputExpr,
    bind_value_expr,
    iter_value_exprs,
    parse_value_binding,
)


DYNAMIC_GRAPH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"request": {"type": "string"}},
    "required": ["request"],
    "additionalProperties": False,
}


def normalize_planner_graph(
    graph: PlannerGraph,
    *,
    spec_id: str,
    version: int,
    capabilities: Iterable[CapabilityDefinition],
    current: DAGSpec | None = None,
    input_schema: dict[str, Any] | None = None,
) -> DAGSpec:
    """Resolve planner-owned intent against the host capability catalog."""
    definitions = {definition.id: definition for definition in capabilities}
    try:
        spec = _normalize_graph(
            graph,
            spec_id=spec_id,
            version=version,
            definitions=definitions,
            current=current,
            input_schema=input_schema or DYNAMIC_GRAPH_INPUT_SCHEMA,
        )
        validate_dag_spec(spec)
        _validate_output_paths(spec, definitions)
    except DAGCreationError:
        raise
    except (DAGValidationError, ValueError) as exc:
        raise DAGCreationError(str(exc)) from exc
    return spec


def normalize_builder_dag(
    dag: Dag,
    *,
    spec_id: str,
    version: int,
    capabilities: Iterable[CapabilityDefinition],
    current: DAGSpec | None = None,
    input_schema: dict[str, Any] | None = None,
) -> DAGSpec:
    """Normalize an AST-translated public Builder graph into a dynamic DAGSpec."""
    definitions = {definition.id: definition for definition in capabilities}
    try:
        spec = _normalize_builder_spec(
            dag.to_dag_spec(),
            spec_id=spec_id,
            version=version,
            definitions=definitions,
            current=current,
            input_schema=input_schema or DYNAMIC_GRAPH_INPUT_SCHEMA,
        )
        _resolve_spec_invocations(spec, definitions)
        _preserve_invocation_ids(spec, current)
        validate_dag_spec(spec)
        _validate_output_paths(spec, definitions)
    except DAGCreationError:
        raise
    except (DAGValidationError, TypeError, ValueError) as exc:
        raise DAGCreationError(str(exc)) from exc
    return spec


def resolve_dag_spec_capabilities(
    spec: DAGSpec,
    capabilities: Iterable[CapabilityDefinition],
) -> DAGSpec:
    """Re-apply host-owned capability metadata to a reviewed DAGSpec."""
    resolved = spec.model_copy(deep=True)
    definitions = {definition.id: definition for definition in capabilities}
    try:
        _resolve_spec_invocations(resolved, definitions)
        validate_dag_spec(resolved)
        _validate_output_paths(resolved, definitions)
    except DAGCreationError:
        raise
    except (DAGValidationError, ValueError) as exc:
        raise DAGCreationError(str(exc)) from exc
    return resolved


def planner_value_to_native(value: PlannerValue) -> Any:
    """Convert the strict planner Value AST into DAGSpec-native values."""
    if isinstance(value, PlannerLiteralValue):
        return value.value
    if isinstance(value, PlannerListValue):
        return [planner_value_to_native(item) for item in value.items]
    if isinstance(value, PlannerObjectValue):
        return _named_values_to_dict(value.entries)
    if isinstance(value, PlannerGraphInputValue):
        return bind_value_expr({"type": "graph_input", "path": value.path})
    if isinstance(value, PlannerNodeOutputValue):
        return bind_value_expr({
            "type": "node_output",
            "node_id": value.node_id,
            "field": value.field,
            "path": value.path,
        })
    if isinstance(value, PlannerArtifactValue):
        return bind_value_expr({
            "type": "artifact",
            "artifact_id": value.artifact_id,
            "field": value.field,
        })
    if isinstance(value, PlannerFormatValue):
        return bind_value_expr({
            "type": "format",
            "template": value.template,
            "values": _named_values_to_dict(value.values),
        })
    if isinstance(value, PlannerCompareValue):
        return bind_value_expr({
            "type": "compare",
            "op": value.op,
            "left": planner_value_to_native(value.left),
            "right": planner_value_to_native(value.right),
        })
    if isinstance(value, PlannerItemValue):
        return bind_value_expr({"type": "item", "path": value.path})
    raise TypeError(f"Unsupported planner value: {type(value).__name__}.")


def _normalize_graph(
    graph: PlannerGraph,
    *,
    spec_id: str,
    version: int,
    definitions: dict[str, CapabilityDefinition],
    current: DAGSpec | None,
    input_schema: dict[str, Any],
) -> DAGSpec:
    if not graph.nodes:
        raise DAGCreationError("propose_plan requires at least one node.")
    current_nodes = {node.id: node for node in current.nodes} if current is not None else {}
    nodes: list[DAGNode] = []
    for planner_node in graph.nodes:
        current_node = current_nodes.get(planner_node.id)
        if isinstance(planner_node, PlannerCapabilityNode):
            definition = _definition(definitions, planner_node.capability_id)
            arguments = _normalized_arguments(planner_node.arguments, definition)
            invocation = _invocation(
                definition,
                arguments,
                current_node=current_node,
                map_node=False,
            )
            payload = CapabilityNodePayload(type="capability", invocation=invocation)
        elif isinstance(planner_node, PlannerMapNode):
            definition = _definition(definitions, planner_node.capability_id)
            arguments = _normalized_arguments(planner_node.arguments, definition)
            invocation = _invocation(
                definition,
                arguments,
                current_node=current_node,
                map_node=True,
            )
            payload = MapNodePayload(
                type="map",
                items=planner_value_to_native(planner_node.items),
                invocation=invocation,
                max_items=planner_node.max_items,
                max_concurrency=planner_node.max_concurrency,
            )
        elif isinstance(planner_node, PlannerSubgraphNode):
            current_spec = (
                current_node.payload.spec
                if current_node is not None and isinstance(current_node.payload, SubgraphNodePayload)
                else None
            )
            payload = SubgraphNodePayload(
                type="subgraph",
                spec=_normalize_graph(
                    planner_node.graph,
                    spec_id=f"{spec_id}.{planner_node.id}.subgraph",
                    version=version,
                    definitions=definitions,
                    current=current_spec,
                    input_schema=_planner_value_schema(
                        planner_node.input,
                        nodes=graph.nodes,
                        definitions=definitions,
                        input_schema=input_schema,
                    ),
                ),
                input=None if planner_node.input is None else planner_value_to_native(planner_node.input),
            )
        elif isinstance(planner_node, PlannerLoopNode):
            current_spec = (
                current_node.payload.body
                if current_node is not None and isinstance(current_node.payload, LoopNodePayload)
                else None
            )
            payload = LoopNodePayload(
                type="loop",
                body=_normalize_graph(
                    planner_node.body,
                    spec_id=f"{spec_id}.{planner_node.id}.loop",
                    version=version,
                    definitions=definitions,
                    current=current_spec,
                    input_schema=_planner_value_schema(
                        planner_node.input,
                        nodes=graph.nodes,
                        definitions=definitions,
                        input_schema=input_schema,
                    ),
                ),
                until=planner_value_to_native(planner_node.until),
                max_iterations=planner_node.max_iterations,
                input=None if planner_node.input is None else planner_value_to_native(planner_node.input),
            )
        else:
            raise TypeError(f"Unsupported planner node: {type(planner_node).__name__}.")
        nodes.append(DAGNode(
            id=planner_node.id,
            title=planner_node.title,
            payload=payload,
            status="planned",
            inputs=list(planner_node.inputs),
            outputs=list(planner_node.outputs),
        ))

    artifacts = {
        artifact.id: Artifact(
            id=artifact.id,
            paths=list(artifact.paths),
            description=artifact.description,
            required=artifact.required,
            metadata={},
        )
        for artifact in graph.artifacts
    }
    edges = [
        DAGEdge(
            source=edge.source,
            target=edge.target,
            reason=edge.reason,
            when=None if edge.when is None else planner_value_to_native(edge.when),
        )
        for edge in graph.edges
    ]
    _add_internal_start(nodes, edges)
    return DAGSpec(
        id=spec_id,
        name=graph.name or spec_id,
        version=version,
        description=graph.description,
        input_schema=deepcopy(input_schema),
        artifacts=artifacts,
        nodes=nodes,
        edges=edges,
        output=None if graph.output is None else planner_value_to_native(graph.output),
        metadata={},
    )


def _normalize_builder_spec(
    source: DAGSpec,
    *,
    spec_id: str,
    version: int,
    definitions: dict[str, CapabilityDefinition],
    current: DAGSpec | None,
    input_schema: dict[str, Any],
) -> DAGSpec:
    if not source.nodes:
        raise DAGCreationError("propose_plan requires at least one node.")
    spec = source.model_copy(deep=True)
    spec.id = spec_id
    spec.version = version
    spec.input_schema = deepcopy(input_schema)
    spec.metadata = {}
    for artifact in spec.artifacts.values():
        artifact.metadata = {}

    if any(node.id == "start" for node in spec.nodes):
        raise DAGCreationError("Node id 'start' is reserved for runtime bookkeeping.")
    current_nodes = {node.id: node for node in current.nodes} if current is not None else {}
    for node in spec.nodes:
        node.status = "planned"
        payload = node.payload
        current_node = current_nodes.get(node.id)
        if isinstance(payload, SubgraphNodePayload):
            current_spec = (
                current_node.payload.spec
                if current_node is not None
                and isinstance(current_node.payload, SubgraphNodePayload)
                else None
            )
            payload.spec = _normalize_builder_spec(
                payload.spec,
                spec_id=f"{spec_id}.{node.id}.subgraph",
                version=version,
                definitions=definitions,
                current=current_spec,
                input_schema=_native_value_schema(
                    payload.input,
                    nodes=spec.nodes,
                    definitions=definitions,
                    input_schema=input_schema,
                ),
            )
        elif isinstance(payload, LoopNodePayload):
            current_spec = (
                current_node.payload.body
                if current_node is not None
                and isinstance(current_node.payload, LoopNodePayload)
                else None
            )
            payload.body = _normalize_builder_spec(
                payload.body,
                spec_id=f"{spec_id}.{node.id}.loop",
                version=version,
                definitions=definitions,
                current=current_spec,
                input_schema=_native_value_schema(
                    payload.input,
                    nodes=spec.nodes,
                    definitions=definitions,
                    input_schema=input_schema,
                ),
            )
    _add_internal_start(spec.nodes, spec.edges)
    return spec


def _add_internal_start(nodes: list[DAGNode], edges: list[DAGEdge]) -> None:
    incoming = {edge.target for edge in edges}
    roots = [node.id for node in nodes if node.id not in incoming]
    if not roots:
        return
    nodes.insert(0, DAGNode(id="start", payload=StartNodePayload(type="start")))
    edges[:0] = [DAGEdge(source="start", target=node_id) for node_id in roots]


def _definition(
    definitions: dict[str, CapabilityDefinition],
    capability_id: str,
) -> CapabilityDefinition:
    definition = definitions.get(capability_id)
    if definition is None:
        available = ", ".join(sorted(definitions)) or "(none)"
        raise DAGCreationError(
            f"Unknown capability '{capability_id}'. Available capabilities: {available}."
        )
    return definition


def _resolve_spec_invocations(
    spec: DAGSpec,
    definitions: dict[str, CapabilityDefinition],
) -> None:
    for node in spec.nodes:
        payload = node.payload
        if isinstance(payload, (CapabilityNodePayload, MapNodePayload)):
            invocation = payload.invocation
            definition = _definition(definitions, invocation.capability_id)
            arguments = dict(invocation.arguments)
            schema = definition.parameters or {"type": "object"}
            try:
                Draft202012Validator.check_schema(schema)
            except Exception as exc:
                raise DAGCreationError(
                    f"Capability '{definition.id}' publishes an invalid input schema: {exc}"
                ) from exc
            _apply_schema_defaults(arguments, schema)
            errors = sorted(
                _ExpressionAwareValidator(schema).iter_errors(arguments),
                key=lambda error: [str(item) for item in error.absolute_path],
            )
            if errors:
                error = errors[0]
                path = ".".join(str(item) for item in error.absolute_path)
                location = f" at arguments.{path}" if path else ""
                raise DAGCreationError(
                    f"Capability '{definition.id}' has invalid arguments{location}: {error.message}"
                )
            invocation.arguments = arguments
            invocation.kind = definition.kind
            invocation.risk = definition.policy.risk
            invocation.boundary = infer_capability_boundary(definition, arguments)
        elif isinstance(payload, SubgraphNodePayload):
            _resolve_spec_invocations(payload.spec, definitions)
        elif isinstance(payload, LoopNodePayload):
            _resolve_spec_invocations(payload.body, definitions)


def _preserve_invocation_ids(proposed: DAGSpec, current: DAGSpec | None) -> None:
    if current is None:
        return
    current_nodes = {node.id: node for node in current.nodes}
    for node in proposed.nodes:
        current_node = current_nodes.get(node.id)
        if current_node is None:
            continue
        payload = node.payload
        current_payload = current_node.payload
        if isinstance(payload, (CapabilityNodePayload, MapNodePayload)) and isinstance(
            current_payload,
            type(payload),
        ):
            proposed_invocation = payload.invocation
            current_invocation = current_payload.invocation
            _reuse_invocation_id(proposed_invocation, current_invocation)
        elif isinstance(payload, SubgraphNodePayload) and isinstance(
            current_payload,
            SubgraphNodePayload,
        ):
            _preserve_invocation_ids(payload.spec, current_payload.spec)
        elif isinstance(payload, LoopNodePayload) and isinstance(
            current_payload,
            LoopNodePayload,
        ):
            _preserve_invocation_ids(payload.body, current_payload.body)


def _normalized_arguments(
    values: list[PlannerNamedValue],
    definition: CapabilityDefinition,
) -> dict[str, Any]:
    arguments = _named_values_to_dict(values)
    schema = definition.parameters or {"type": "object"}
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise DAGCreationError(
            f"Capability '{definition.id}' publishes an invalid input schema: {exc}"
        ) from exc
    _apply_schema_defaults(arguments, schema)
    errors = sorted(
        _ExpressionAwareValidator(schema).iter_errors(arguments),
        key=lambda error: [str(item) for item in error.absolute_path],
    )
    if errors:
        error = errors[0]
        path = ".".join(str(item) for item in error.absolute_path)
        location = f" at arguments.{path}" if path else ""
        raise DAGCreationError(
            f"Capability '{definition.id}' has invalid arguments{location}: {error.message}"
        )
    return arguments


def _invocation(
    definition: CapabilityDefinition,
    arguments: dict[str, Any],
    *,
    current_node: DAGNode | None,
    map_node: bool,
) -> CapabilityInvocation:
    invocation = CapabilityInvocation(
        capability_id=definition.id,
        kind=definition.kind,
        arguments=arguments,
        boundary=infer_capability_boundary(definition, arguments),
        risk=definition.policy.risk,
    )
    if current_node is None:
        return invocation
    payload = current_node.payload
    current_invocation = (
        payload.invocation
        if (map_node and isinstance(payload, MapNodePayload))
        or (not map_node and isinstance(payload, CapabilityNodePayload))
        else None
    )
    if current_invocation is None:
        return invocation
    _reuse_invocation_id(invocation, current_invocation)
    return invocation


def _reuse_invocation_id(
    proposed: CapabilityInvocation,
    current: CapabilityInvocation,
) -> None:
    if (
        proposed.capability_id == current.capability_id
        and proposed.arguments == current.arguments
        and proposed.boundary == current.boundary
        and proposed.risk == current.risk
    ):
        proposed.invocation_id = current.invocation_id


def _named_values_to_dict(values: list[PlannerNamedValue]) -> dict[str, Any]:
    return {item.name: planner_value_to_native(item.value) for item in values}


def _apply_schema_defaults(instance: Any, schema: dict[str, Any]) -> None:
    if not isinstance(instance, dict):
        return
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    for name, property_schema in properties.items():
        if not isinstance(property_schema, dict):
            continue
        if name not in instance and "default" in property_schema:
            instance[name] = deepcopy(property_schema["default"])
        elif name in instance and not _is_value_binding(instance[name]):
            _apply_schema_defaults(instance[name], property_schema)


def _skip_value_binding(validator_fn):
    def validate(validator, keyword_value, instance, schema):
        if _is_value_binding(instance):
            return
        yield from validator_fn(validator, keyword_value, instance, schema)

    return validate


_ExpressionAwareValidator = validators.extend(
    Draft202012Validator,
    {
        keyword: _skip_value_binding(validator_fn)
        for keyword, validator_fn in Draft202012Validator.VALIDATORS.items()
    },
)


def _is_value_binding(value: Any) -> bool:
    try:
        return parse_value_binding(value) is not None
    except Exception:
        return False


def _validate_output_paths(
    spec: DAGSpec,
    definitions: dict[str, CapabilityDefinition],
) -> None:
    nodes = {node.id: node for node in spec.nodes}
    sources: list[tuple[str, Any]] = []
    for node in spec.nodes:
        payload = node.payload
        if isinstance(payload, CapabilityNodePayload):
            sources.append((f"Node '{node.id}'", payload.invocation.arguments))
        elif isinstance(payload, MapNodePayload):
            sources.extend([
                (f"Map node '{node.id}' items", payload.items),
                (f"Map node '{node.id}' arguments", payload.invocation.arguments),
            ])
        elif isinstance(payload, SubgraphNodePayload):
            sources.append((f"Subgraph node '{node.id}' input", payload.input))
            _validate_output_paths(payload.spec, definitions)
        elif isinstance(payload, LoopNodePayload):
            sources.extend([
                (f"Loop node '{node.id}' input", payload.input),
                (f"Loop node '{node.id}' condition", payload.until),
            ])
            _validate_output_paths(payload.body, definitions)
    sources.extend(
        (f"Edge '{edge.source}->{edge.target}' condition", edge.when)
        for edge in spec.edges
        if edge.when is not None
    )
    sources.append(("DAG output", spec.output))

    for owner, value in sources:
        for expression in iter_value_exprs(value):
            if isinstance(expression, GraphInputExpr):
                if expression.path:
                    _resolve_schema_path(spec.input_schema, expression.path, owner)
            elif isinstance(expression, NodeOutputExpr) and expression.path:
                node = nodes.get(expression.node_id)
                if node is None:
                    continue
                source_schema = _node_field_schema(node, expression.field, definitions)
                _resolve_schema_path(source_schema, expression.path, owner)


def _node_field_schema(
    node: DAGNode,
    field: str,
    definitions: dict[str, CapabilityDefinition],
) -> dict[str, Any]:
    if field == "content":
        return {"type": "string"}
    if field == "status":
        return {"type": "string"}
    if field == "steps":
        return {"type": "array", "items": {}}
    payload = node.payload
    if isinstance(payload, (CapabilityNodePayload, MapNodePayload)):
        definition = definitions.get(payload.invocation.capability_id)
        schema = {} if definition is None else definition.output_schema
        if isinstance(payload, MapNodePayload):
            return {"type": "array", "items": schema}
        return schema
    if isinstance(payload, SubgraphNodePayload):
        return _spec_output_schema(payload.spec, definitions)
    if isinstance(payload, LoopNodePayload):
        return _spec_output_schema(payload.body, definitions)
    return {}


def _spec_output_schema(
    spec: DAGSpec,
    definitions: dict[str, CapabilityDefinition],
) -> dict[str, Any]:
    return _native_value_schema(
        spec.output,
        nodes=spec.nodes,
        definitions=definitions,
        input_schema=spec.input_schema,
    )


def _resolve_schema_path(schema: dict[str, Any], path: list[str | int], owner: str) -> None:
    if not schema:
        raise DAGCreationError(f"{owner} uses a path but the source has no output schema.")
    if _schema_at_path(schema, path) is None:
        joined = ".".join(str(item) for item in path)
        raise DAGCreationError(f"{owner} references unknown output path '{joined}'.")


def _schema_at_path(
    schema: dict[str, Any],
    path: list[str | int],
    *,
    root: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    current: dict[str, Any] = schema
    schema_root = schema if root is None else root
    for item in path:
        current = _resolve_local_ref(current, schema_root)
        alternatives = current.get("anyOf") or current.get("oneOf")
        if isinstance(alternatives, list):
            matches = [
                resolved
                for candidate in alternatives
                if isinstance(candidate, dict)
                for resolved in [_schema_at_path(candidate, [item], root=schema_root)]
                if resolved is not None
            ]
            if not matches:
                return None
            current = matches[0]
            continue
        if isinstance(item, str):
            properties = current.get("properties")
            if not isinstance(properties, dict) or not isinstance(properties.get(item), dict):
                return None
            current = properties[item]
        else:
            items = current.get("items")
            if not isinstance(items, dict):
                return None
            current = items
    return _resolve_local_ref(current, schema_root)


def _resolve_local_ref(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return schema
    current: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(current, dict):
            return schema
        current = current.get(part.replace("~1", "/").replace("~0", "~"))
    return current if isinstance(current, dict) else schema


def _literal_schema(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        return {"type": "array", "items": _literal_schema(value[0]) if value else {}}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {key: _literal_schema(item) for key, item in value.items()},
        }
    return {}


def _native_value_schema(
    value: Any,
    *,
    nodes: list[DAGNode],
    definitions: dict[str, CapabilityDefinition],
    input_schema: dict[str, Any],
) -> dict[str, Any]:
    expression = parse_value_binding(value)
    if isinstance(expression, GraphInputExpr):
        return _schema_at_path(input_schema, expression.path) or {}
    if isinstance(expression, NodeOutputExpr):
        source = next((node for node in nodes if node.id == expression.node_id), None)
        if source is None:
            return {}
        schema = _node_field_schema(source, expression.field, definitions)
        return _schema_at_path(schema, expression.path) or {}
    if isinstance(expression, ArtifactExpr):
        if expression.field in {"paths", "absolute_paths"}:
            return {"type": "array", "items": {"type": "string"}}
        return {"type": "string"}
    if isinstance(expression, FormatExpr):
        return {"type": "string"}
    if isinstance(expression, CompareExpr):
        return {"type": "boolean"}
    if isinstance(expression, ItemExpr):
        return {}
    if isinstance(value, list):
        return {
            "type": "array",
            "items": (
                _native_value_schema(
                    value[0],
                    nodes=nodes,
                    definitions=definitions,
                    input_schema=input_schema,
                )
                if value
                else {}
            ),
        }
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {
                key: _native_value_schema(
                    item,
                    nodes=nodes,
                    definitions=definitions,
                    input_schema=input_schema,
                )
                for key, item in value.items()
            },
            "required": list(value),
        }
    return _literal_schema(value)


def _planner_value_schema(
    value: PlannerValue | None,
    *,
    nodes: list[Any],
    definitions: dict[str, CapabilityDefinition],
    input_schema: dict[str, Any],
) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, PlannerLiteralValue):
        return _literal_schema(value.value)
    if isinstance(value, PlannerListValue):
        return {
            "type": "array",
            "items": (
                _planner_value_schema(
                    value.items[0],
                    nodes=nodes,
                    definitions=definitions,
                    input_schema=input_schema,
                )
                if value.items
                else {}
            ),
        }
    if isinstance(value, PlannerObjectValue):
        return {
            "type": "object",
            "properties": {
                entry.name: _planner_value_schema(
                    entry.value,
                    nodes=nodes,
                    definitions=definitions,
                    input_schema=input_schema,
                )
                for entry in value.entries
            },
            "required": [entry.name for entry in value.entries],
        }
    if isinstance(value, PlannerGraphInputValue):
        return _schema_at_path(input_schema, value.path) or {}
    if isinstance(value, PlannerNodeOutputValue):
        source = next((node for node in nodes if node.id == value.node_id), None)
        schema = _planner_node_output_schema(source, value.field, definitions)
        return _schema_at_path(schema, value.path) or {}
    if isinstance(value, PlannerArtifactValue):
        return {
            "type": "array" if value.field in {"paths", "absolute_paths"} else "string",
            **(
                {"items": {"type": "string"}}
                if value.field in {"paths", "absolute_paths"}
                else {}
            ),
        }
    if isinstance(value, PlannerFormatValue):
        return {"type": "string"}
    if isinstance(value, PlannerCompareValue):
        return {"type": "boolean"}
    return {}


def _planner_node_output_schema(
    node: Any,
    field: str,
    definitions: dict[str, CapabilityDefinition],
) -> dict[str, Any]:
    if field == "content":
        return {"type": "string"}
    if field == "status":
        return {"type": "string"}
    if field == "steps":
        return {"type": "array", "items": {}}
    if isinstance(node, PlannerCapabilityNode):
        definition = definitions.get(node.capability_id)
        return {} if definition is None else definition.output_schema
    if isinstance(node, PlannerMapNode):
        definition = definitions.get(node.capability_id)
        return {
            "type": "array",
            "items": {} if definition is None else definition.output_schema,
        }
    if isinstance(node, PlannerSubgraphNode):
        return _planner_graph_output_schema(node.graph, definitions)
    if isinstance(node, PlannerLoopNode):
        return _planner_graph_output_schema(node.body, definitions)
    return {}


def _planner_graph_output_schema(
    graph: PlannerGraph,
    definitions: dict[str, CapabilityDefinition],
) -> dict[str, Any]:
    if graph.output is None:
        return {"type": "null"}
    return _planner_value_schema(
        graph.output,
        nodes=graph.nodes,
        definitions=definitions,
        input_schema={},
    )
