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
    DAGEdge,
    DAGNode,
    DAGSpec,
)
from dagent.schemas.common import json_schema_for_type
from dagent.schemas.value import (
    ArtifactExpr,
    FormatExpr,
    GraphInputExpr,
    NodeOutputExpr,
    bind_value_expr,
    iter_artifact_exprs,
)


ValuePathItem = str | int
NodeOutputField = Literal["value", "content", "status", "steps"]
ArtifactField = Literal["path", "paths", "absolute_path", "absolute_paths"]


@dataclass(frozen=True)
class InputRef:
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


@dataclass(frozen=True)
class NodeOutputRef:
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
    """User-facing DAG node definition."""

    def __init__(
        self,
        id: str,
        *,
        target: CapabilityBinding | ToolAgent | str,
        inputs: dict[str, Any] | None = None,
        artifact_inputs: list[ArtifactRef | str] | None = None,
        artifact_outputs: list[ArtifactRef | str] | None = None,
        title: str | None = None,
        boundary: Boundary | None = None,
    ) -> None:
        self.id = id
        self.target = target
        self.inputs = dict(inputs or {})
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
        self._nodes: dict[str, DAGNode] = {}
        self._edges: list[DAGEdge] = []
        self._artifacts: dict[str, Artifact] = {}
        self._capabilities: dict[str, CapabilityBinding] = {}
        self._agents: dict[str, ToolAgent] = {}

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
        capability_id, kind, risk = self._target_invocation_metadata(node.target)
        arguments = _normalize_arguments(node.inputs)
        boundary = node.boundary or Boundary(mode="read_only", allowed_paths=["."])
        output_ids = set(_artifact_ids(node.artifact_outputs))
        explicit_input_ids = set(_artifact_ids(node.artifact_inputs))
        inferred_input_ids = _artifact_ids_from_values(
            arguments,
            boundary.allowed_paths,
            boundary.allowed_commands,
        ) - output_ids
        self._nodes[node.id] = DAGNode(
            id=node.id,
            title=node.title or _title_from_id(node.id),
            payload={
                "type": "capability",
                "invocation": CapabilityInvocation(
                    capability_id=capability_id,
                    kind=kind,  # type: ignore[arg-type]
                    arguments=arguments,
                    boundary=boundary,
                    risk=risk,  # type: ignore[arg-type]
                ),
            },
            inputs=sorted(explicit_input_ids | inferred_input_ids),
            outputs=sorted(output_ids),
        )
        return node

    def add_edge(
        self,
        source: Node | str,
        target: Node | str,
        *,
        reason: str = "",
    ) -> "Dag":
        return self.edge(source, target, reason=reason)

    def edge(
        self,
        source: Node | str,
        target: Node | str,
        *,
        reason: str = "",
    ) -> "Dag":
        source_id = _node_id(source)
        target_id = _node_id(target)
        self._ensure_node(source_id)
        self._ensure_node(target_id)
        edge = DAGEdge(source=source_id, target=target_id, reason=reason)
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
    if isinstance(value, (InputRef, NodeOutputRef, ArtifactValueRef, FormatRef)):
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
    if kind in {"tool", "mcp", "skill", "shell", "agent", "memory", "file"}:
        return kind  # type: ignore[return-value]
    raise ValueError(f"Cannot infer capability kind from id '{capability_id}'.")
