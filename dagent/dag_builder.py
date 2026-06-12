"""User-facing DAG builder and static DAG runner."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from dagent.agent import ToolAgent
from dagent.capabilities.decorator import CapabilityBinding
from dagent.schemas import (
    Artifact,
    Boundary,
    CapabilityInvocation,
    CapabilityKind,
    CapabilityNodePayload,
    DAGEdge,
    DAGNode,
    DAGSpec,
    LoopNodePayload,
    MapNodePayload,
    NodePayload,
    SubgraphNodePayload,
)
from dagent.schemas.common import json_schema_for_type
from dagent.schemas.value import (
    ArtifactExpr,
    CompareExpr,
    CompareOp,
    FormatExpr,
    GraphInputExpr,
    ItemExpr,
    NodeOutputExpr,
    bind_value_expr,
    iter_artifact_exprs,
)


ValuePathItem = str | int
NodeOutputField = Literal["value", "content", "status", "steps"]
ArtifactField = Literal["path", "paths", "absolute_path", "absolute_paths"]


class _Comparable:
    """Comparison operators that build edge/loop condition references."""

    __hash__ = None

    def __eq__(self, other: Any) -> "CompareRef":
        return CompareRef(self, "eq", other)

    def __ne__(self, other: Any) -> "CompareRef":
        return CompareRef(self, "ne", other)

    def __gt__(self, other: Any) -> "CompareRef":
        return CompareRef(self, "gt", other)

    def __ge__(self, other: Any) -> "CompareRef":
        return CompareRef(self, "ge", other)

    def __lt__(self, other: Any) -> "CompareRef":
        return CompareRef(self, "lt", other)

    def __le__(self, other: Any) -> "CompareRef":
        return CompareRef(self, "le", other)


@dataclass(frozen=True, eq=False)
class InputRef(_Comparable):
    """Reference to the DAG run input."""

    _path: tuple[ValuePathItem, ...] = ()

    def __getitem__(self, item: ValuePathItem) -> "InputRef":
        return replace(self, _path=(*self._path, item))

    def __getattr__(self, name: str) -> "InputRef":
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def as_expr(self) -> dict[str, Any]:
        return bind_value_expr(GraphInputExpr(type="graph_input", path=list(self._path)))


@dataclass(frozen=True, eq=False)
class NodeOutputRef(_Comparable):
    """Reference to a completed DAG node result."""

    _node_id: str
    _field: NodeOutputField = "value"
    _path: tuple[ValuePathItem, ...] = ()

    def __getitem__(self, item: ValuePathItem) -> "NodeOutputRef":
        return replace(self, _path=(*self._path, item))

    def __getattr__(self, name: str) -> "NodeOutputRef":
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def as_expr(self) -> dict[str, Any]:
        return bind_value_expr(
            NodeOutputExpr(
                type="node_output",
                node_id=self._node_id,
                field=self._field,
                path=list(self._path),
            )
        )


@dataclass(frozen=True, eq=False)
class ItemRef(_Comparable):
    """The current map item, or the latest loop body output in loop conditions."""

    _path: tuple[ValuePathItem, ...] = ()

    def __getitem__(self, item: ValuePathItem) -> "ItemRef":
        return replace(self, _path=(*self._path, item))

    def __getattr__(self, name: str) -> "ItemRef":
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def as_expr(self) -> dict[str, Any]:
        return bind_value_expr(ItemExpr(type="map_item", path=list(self._path)))


item = ItemRef()


@dataclass(frozen=True)
class CompareRef:
    """Comparison of two values after nested DAG references resolve."""

    left: Any
    op: CompareOp
    right: Any

    def as_expr(self) -> dict[str, Any]:
        return bind_value_expr(
            CompareExpr(
                type="compare",
                op=self.op,
                left=_normalize_value(self.left),
                right=_normalize_value(self.right),
            )
        )


@dataclass(frozen=True)
class ArtifactValueRef:
    """Reference to a DAG artifact field."""

    _artifact_id: str
    _field: ArtifactField = "path"

    def as_expr(self) -> dict[str, Any]:
        return bind_value_expr(
            ArtifactExpr(
                type="artifact",
                artifact_id=self._artifact_id,
                field=self._field,
            )
        )


@dataclass(frozen=True)
class FormatRef:
    """Format a string after resolving nested DAG value references."""

    template: str
    values: dict[str, Any]

    def as_expr(self) -> dict[str, Any]:
        return bind_value_expr(
            FormatExpr(
                type="format",
                template=self.template,
                values={key: _normalize_value(value) for key, value in self.values.items()},
            )
        )


@dataclass(frozen=True)
class ArtifactRef:
    """Reference to a DAG artifact."""

    id: str

    @property
    def path(self) -> ArtifactValueRef:
        return ArtifactValueRef(self.id, "path")

    @property
    def paths(self) -> ArtifactValueRef:
        return ArtifactValueRef(self.id, "paths")

    @property
    def absolute_path(self) -> ArtifactValueRef:
        return ArtifactValueRef(self.id, "absolute_path")

    @property
    def absolute_paths(self) -> ArtifactValueRef:
        return ArtifactValueRef(self.id, "absolute_paths")


class Node:
    """User-facing DAG node definition.

    ``target`` may be a capability binding, a ``ToolAgent``, a capability id
    string, or a ``Dag`` (executed as an embedded subgraph). For subgraph
    targets ``inputs`` is the child graph input and may be any value.
    """

    def __init__(
        self,
        id: str,
        *,
        target: "CapabilityBinding | ToolAgent | Dag | str",
        inputs: Any = None,
        artifact_inputs: list[ArtifactRef | str] | None = None,
        artifact_outputs: list[ArtifactRef | str] | None = None,
        title: str | None = None,
        boundary: Boundary | None = None,
    ) -> None:
        self.id = id
        self.target = target
        self.inputs = inputs
        self.artifact_inputs = list(artifact_inputs or [])
        self.artifact_outputs = list(artifact_outputs or [])
        self.title = title
        self.boundary = boundary

    @property
    def output(self) -> NodeOutputRef:
        return NodeOutputRef(self.id, "value")

    @property
    def content(self) -> NodeOutputRef:
        return NodeOutputRef(self.id, "content")

    @property
    def status(self) -> NodeOutputRef:
        return NodeOutputRef(self.id, "status")

    @property
    def steps(self) -> NodeOutputRef:
        return NodeOutputRef(self.id, "steps")


class MapNode(Node):
    """Fan one capability call out over a list resolved at runtime.

    ``over`` must resolve to a list; ``inputs`` may reference the current
    element through ``dagent.item``. The node value is the list of per-item
    values, in item order.
    """

    def __init__(
        self,
        id: str,
        *,
        target: CapabilityBinding | ToolAgent | str,
        over: Any,
        inputs: dict[str, Any] | None = None,
        max_items: int = 64,
        max_concurrency: int = 8,
        artifact_inputs: list[ArtifactRef | str] | None = None,
        artifact_outputs: list[ArtifactRef | str] | None = None,
        title: str | None = None,
        boundary: Boundary | None = None,
    ) -> None:
        super().__init__(
            id,
            target=target,
            inputs=inputs,
            artifact_inputs=artifact_inputs,
            artifact_outputs=artifact_outputs,
            title=title,
            boundary=boundary,
        )
        self.over = over
        self.max_items = max_items
        self.max_concurrency = max_concurrency


class LoopNode(Node):
    """Run a ``Dag`` body repeatedly until ``until`` is truthy.

    Each iteration's output feeds the next iteration's graph input; ``until``
    is evaluated against the latest output via ``dagent.item``. The node value
    is the last iteration's output, even when ``max_iterations`` is exhausted.
    """

    def __init__(
        self,
        id: str,
        *,
        body: "Dag",
        until: Any,
        max_iterations: int,
        input: Any = None,
        title: str | None = None,
    ) -> None:
        super().__init__(id, target=body, inputs=input, title=title)
        self.until = until
        self.max_iterations = max_iterations


class Dag:
    """Builder for static DAGSpec definitions."""

    def __init__(
        self,
        id: str,
        *,
        name: str | None = None,
        description: str = "",
        version: int = 1,
        input: Any = None,
        input_schema: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.id = id
        self.name = name or id
        self.description = description
        self.version = version
        self.input_schema = dict(input_schema) if input_schema is not None else json_schema_for_type(input)
        self.metadata = dict(metadata or {})
        self.input = InputRef()
        self._output: Any = None
        self._nodes: dict[str, DAGNode] = {}
        self._edges: list[DAGEdge] = []
        self._artifacts: dict[str, Artifact] = {}
        self._capabilities: dict[str, CapabilityBinding] = {}
        self._agents: dict[str, ToolAgent] = {}

    @property
    def output(self) -> Any:
        """The DAG's declared result; used as the node value when run as a subgraph."""
        return self._output

    @output.setter
    def output(self, value: Any) -> None:
        self._output = _normalize_value(value)

    def artifact(
        self,
        id: str,
        paths: str | list[str],
        *,
        description: str = "",
        required: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        if id in self._artifacts:
            raise ValueError(f"Artifact '{id}' already exists.")
        path_list = [paths] if isinstance(paths, str) else list(paths)
        self._artifacts[id] = Artifact(
            id=id,
            paths=path_list,
            description=description,
            required=required,
            metadata=metadata or {},
        )
        return ArtifactRef(id)

    def add_artifact(self, artifact: Artifact) -> ArtifactRef:
        if artifact.id in self._artifacts:
            raise ValueError(f"Artifact '{artifact.id}' already exists.")
        self._artifacts[artifact.id] = artifact.model_copy(deep=True)
        return ArtifactRef(artifact.id)

    def add_node(self, node: Node) -> Node:
        if not isinstance(node, Node):
            raise TypeError("add_node expects a dagent.Node.")
        if node.id in self._nodes:
            raise ValueError(f"Node '{node.id}' already exists.")
        payload, inference_sources = self._node_payload(node)
        output_ids = set(_artifact_ids(node.artifact_outputs))
        explicit_input_ids = set(_artifact_ids(node.artifact_inputs))
        inferred_input_ids = _artifact_ids_from_values(*inference_sources) - output_ids
        self._nodes[node.id] = DAGNode(
            id=node.id,
            title=node.title or _title_from_id(node.id),
            payload=payload,
            inputs=sorted(explicit_input_ids | inferred_input_ids),
            outputs=sorted(output_ids),
        )
        return node

    def add_edge(
        self,
        source: Node | str,
        target: Node | str,
        *,
        when: Any = None,
        reason: str = "",
    ) -> "Dag":
        source_id = _node_id(source)
        target_id = _node_id(target)
        self._ensure_node(source_id)
        self._ensure_node(target_id)
        edge = DAGEdge(
            source=source_id,
            target=target_id,
            reason=reason,
            when=None if when is None else _normalize_value(when),
        )
        if edge not in self._edges:
            self._edges.append(edge)
        return self

    def to_dag_spec(self) -> DAGSpec:
        return DAGSpec(
            id=self.id,
            name=self.name,
            version=self.version,
            description=self.description,
            input_schema=dict(self.input_schema),
            artifacts=dict(self._artifacts),
            nodes=list(self._nodes.values()),
            edges=list(self._edges),
            output=self._output,
            metadata=dict(self.metadata),
        )

    @property
    def capabilities(self) -> list[CapabilityBinding]:
        return list(self._capabilities.values())

    @property
    def agents(self) -> list[ToolAgent]:
        return list(self._agents.values())

    def format(self, template: str, **values: Any) -> FormatRef:
        return FormatRef(template, values)

    def _ensure_node(self, node_id: str) -> None:
        if node_id not in self._nodes:
            raise ValueError(f"Unknown node '{node_id}'.")

    def _node_payload(self, node: Node) -> tuple[NodePayload, list[Any]]:
        """Build the node payload and the values used for artifact-input inference."""
        if isinstance(node, LoopNode):
            if node.boundary is not None:
                raise ValueError("Loop nodes do not accept a boundary; set boundaries on body nodes.")
            body = self._absorb_subgraph(node.target)
            input_value = _normalize_value(node.inputs)
            payload = LoopNodePayload(
                type="loop",
                body=body,
                until=_normalize_value(node.until),
                max_iterations=node.max_iterations,
                input=input_value,
            )
            return payload, [input_value]
        if isinstance(node, MapNode):
            invocation, boundary = self._capability_invocation(node)
            over = _normalize_value(node.over)
            payload = MapNodePayload(
                type="map",
                items=over,
                invocation=invocation,
                max_items=node.max_items,
                max_concurrency=node.max_concurrency,
            )
            return payload, [over, invocation.arguments, boundary.allowed_paths, boundary.allowed_commands]
        if isinstance(node.target, Dag):
            if node.boundary is not None:
                raise ValueError("Subgraph nodes do not accept a boundary; set boundaries on child nodes.")
            spec = self._absorb_subgraph(node.target)
            input_value = _normalize_value(node.inputs)
            payload = SubgraphNodePayload(type="subgraph", spec=spec, input=input_value)
            return payload, [input_value]
        invocation, boundary = self._capability_invocation(node)
        return (
            CapabilityNodePayload(type="capability", invocation=invocation),
            [invocation.arguments, boundary.allowed_paths, boundary.allowed_commands],
        )

    def _capability_invocation(self, node: Node) -> tuple[CapabilityInvocation, Boundary]:
        if isinstance(node.target, Dag):
            raise TypeError(f"Node '{node.id}' target cannot be a Dag here.")
        capability_id, kind, risk = self._target_invocation_metadata(node.target)
        inputs = node.inputs if node.inputs is not None else {}
        if not isinstance(inputs, dict):
            raise TypeError(f"Node '{node.id}' inputs must be a dict for capability targets.")
        arguments = _normalize_arguments(inputs)
        boundary = node.boundary or Boundary(mode="read_only", allowed_paths=["."])
        invocation = CapabilityInvocation(
            capability_id=capability_id,
            kind=kind,  # type: ignore[arg-type]
            arguments=arguments,
            boundary=boundary,
            risk=risk,  # type: ignore[arg-type]
        )
        return invocation, boundary

    def _absorb_subgraph(self, child: "Dag") -> DAGSpec:
        """Embed a child Dag's spec and take over its capability/agent registrations."""
        if not isinstance(child, Dag):
            raise TypeError("Subgraph and loop bodies must be dagent.Dag instances.")
        for capability_id, binding in child._capabilities.items():
            existing = self._capabilities.get(capability_id)
            if existing is not None and existing != binding:
                raise ValueError(f"Capability '{capability_id}' is already bound with different config.")
            self._capabilities[capability_id] = binding
        for name, agent in child._agents.items():
            existing = self._agents.get(name)
            if existing is not None and existing != agent:
                raise ValueError(f"Agent '{name}' already exists with different config.")
            self._agents[name] = agent
        return child.to_dag_spec()

    def _target_invocation_metadata(
        self,
        target: CapabilityBinding | ToolAgent | str,
    ) -> tuple[str, str, str]:
        if isinstance(target, CapabilityBinding):
            capability_id = target.definition.id
            self._capabilities[capability_id] = target
            return capability_id, target.definition.kind, target.definition.policy.risk
        if isinstance(target, ToolAgent):
            agent_name = target.name or ""
            existing = self._agents.get(agent_name)
            if existing is not None and existing != target:
                raise ValueError(f"Agent '{agent_name}' already exists with different config.")
            self._agents[agent_name] = target
            return f"agent.{agent_name}", "agent", "medium"
        if isinstance(target, str):
            kind = _capability_kind_from_id(target)
            if kind == "skill":
                raise ValueError("Node target cannot be a skill capability; attach skills to an agent instead.")
            risk = "medium" if kind == "agent" else "low"
            return target, kind, risk
        raise TypeError("Node target must be a CapabilityBinding, ToolAgent, or capability id string.")


def _artifact_ids(values: list[ArtifactRef | str] | None) -> list[str]:
    return [value.id if isinstance(value, ArtifactRef) else str(value) for value in values or []]


def _node_id(value: Node | str) -> str:
    return value.id if isinstance(value, Node) else str(value)


def _normalize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return {key: _normalize_value(value) for key, value in arguments.items()}


def _normalize_value(value: Any) -> Any:
    if isinstance(value, Node):
        return value.output.as_expr()
    if isinstance(value, ArtifactRef):
        return value.path.as_expr()
    if isinstance(value, (InputRef, NodeOutputRef, ArtifactValueRef, FormatRef, ItemRef, CompareRef)):
        return value.as_expr()
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_value(item) for item in value]
    return value


def _artifact_ids_from_values(*values: Any) -> set[str]:
    return {
        expr.artifact_id
        for value in values
        for expr in iter_artifact_exprs(value)
    }


def _title_from_id(node_id: str) -> str:
    return node_id.replace("_", " ").capitalize()


def _capability_kind_from_id(capability_id: str) -> CapabilityKind:
    kind = capability_id.split(".", 1)[0]
    if kind in {"tool", "mcp", "skill", "agent", "memory"}:
        return kind  # type: ignore[return-value]
    raise ValueError(f"Cannot infer capability kind from id '{capability_id}'.")
