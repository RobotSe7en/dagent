"""User-facing DAG builder and static DAG runner."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from dagent.agent import ToolAgent
from dagent.capabilities.decorator import CapabilityBinding
from dagent.schemas import (
    Artifact,
    ArtifactExpr,
    Boundary,
    CapabilityInvocation,
    CapabilityKind,
    DAGEdge,
    DAGNode,
    DAGSpec,
    FormatExpr,
    GraphInputExpr,
    NodeOutputExpr,
)
from dagent.schemas.common import json_schema_for_type
from dagent.schemas.value import bind_value_expr


ValuePathItem = str | int
NodeOutputField = Literal["value", "content", "status", "steps"]
ArtifactField = Literal["path", "paths", "absolute_path", "absolute_paths"]


@dataclass(frozen=True)
class InputRef:
    """Reference to the DAG run input."""

    path: tuple[ValuePathItem, ...] = ()

    def __getitem__(self, item: ValuePathItem) -> "InputRef":
        return replace(self, path=(*self.path, item))

    def __getattr__(self, name: str) -> "InputRef":
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def as_expr(self) -> dict[str, Any]:
        return bind_value_expr(GraphInputExpr(type="graph_input", path=list(self.path)))


@dataclass(frozen=True)
class NodeOutputRef:
    """Reference to a completed DAG node result."""

    node_id: str
    field: NodeOutputField = "value"
    path: tuple[ValuePathItem, ...] = ()

    def __getitem__(self, item: ValuePathItem) -> "NodeOutputRef":
        return replace(self, path=(*self.path, item))

    def __getattr__(self, name: str) -> "NodeOutputRef":
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def as_expr(self) -> dict[str, Any]:
        return bind_value_expr(
            NodeOutputExpr(
                type="node_output",
                node_id=self.node_id,
                field=self.field,
                path=list(self.path),
            )
        )


@dataclass(frozen=True)
class ArtifactValueRef:
    """Reference to a DAG artifact field."""

    artifact_id: str
    field: ArtifactField = "path"

    def as_expr(self) -> dict[str, Any]:
        return bind_value_expr(
            ArtifactExpr(
                type="artifact",
                artifact_id=self.artifact_id,
                field=self.field,
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


class NodeRef:
    """Reference to a node inside a user-built DAG."""

    def __init__(self, dag: "Dag", node_id: str) -> None:
        self._dag = dag
        self.id = node_id

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

    def after(self, *parents: "NodeRef | str") -> "NodeRef":
        for parent in parents:
            self._dag.edge(parent, self)
        return self

    def then(self, *children: "NodeRef | str") -> "NodeRef":
        for child in children:
            self._dag.edge(self, child)
        return self


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
    ) -> None:
        self.id = id
        self.name = name or id
        self.description = description
        self.version = version
        self.input_schema = json_schema_for_type(input)
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

    def capability_node(
        self,
        id: str,
        capability: CapabilityBinding | str,
        *,
        node_title: str | None = None,
        inputs: list[ArtifactRef | str] | None = None,
        outputs: list[ArtifactRef | str] | None = None,
        boundary: Boundary | None = None,
        **arguments: Any,
    ) -> NodeRef:
        if isinstance(capability, CapabilityBinding):
            capability_id = capability.definition.id
            kind = capability.definition.kind
            risk = capability.definition.policy.risk
            self._capabilities[capability_id] = capability
        elif isinstance(capability, str):
            capability_id = capability
            kind = _capability_kind_from_id(capability_id)
            risk = "low"
        else:
            raise TypeError("capability_node expects a CapabilityBinding or capability id string.")
        return self._add_capability_node(
            id,
            capability_id=capability_id,
            kind=kind,
            risk=risk,
            arguments=_normalize_arguments(arguments),
            title=node_title,
            inputs=inputs,
            outputs=outputs,
            boundary=boundary,
        )

    def agent_node(
        self,
        id: str,
        agent: ToolAgent,
        *,
        prompt: Any,
        max_steps: int | None = None,
        node_title: str | None = None,
        inputs: list[ArtifactRef | str] | None = None,
        outputs: list[ArtifactRef | str] | None = None,
        boundary: Boundary | None = None,
    ) -> NodeRef:
        if not isinstance(agent, ToolAgent):
            raise TypeError("agent_node expects a dagent.ToolAgent.")
        agent_name = agent.name or ""
        existing = self._agents.get(agent_name)
        if existing is not None and existing != agent:
            raise ValueError(f"Agent '{agent_name}' already exists with different config.")
        capability_id = f"agent.{agent_name}"
        resolved_max_steps = agent.max_steps if max_steps is None else max_steps
        if resolved_max_steps < 1:
            raise ValueError("max_steps must be at least 1.")
        self._agents[agent_name] = agent
        return self._add_capability_node(
            id,
            capability_id=capability_id,
            kind="agent",
            risk="medium",
            arguments=_normalize_arguments({"prompt": prompt, "max_steps": resolved_max_steps}),
            title=node_title,
            inputs=inputs,
            outputs=outputs,
            boundary=boundary,
        )

    def edge(
        self,
        source: NodeRef | str,
        target: NodeRef | str,
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
        )

    @property
    def capabilities(self) -> list[CapabilityBinding]:
        return list(self._capabilities.values())

    @property
    def agents(self) -> list[ToolAgent]:
        return list(self._agents.values())

    def format(self, template: str, **values: Any) -> FormatRef:
        return FormatRef(template, values)

    def _add_capability_node(
        self,
        id: str,
        *,
        capability_id: str,
        kind: str,
        risk: str,
        arguments: dict[str, Any],
        title: str | None,
        inputs: list[ArtifactRef | str] | None,
        outputs: list[ArtifactRef | str] | None,
        boundary: Boundary | None,
    ) -> NodeRef:
        if id in self._nodes:
            raise ValueError(f"Node '{id}' already exists.")
        self._nodes[id] = DAGNode(
            id=id,
            title=title or _title_from_id(id),
            payload={
                "type": "capability",
                "invocation": CapabilityInvocation(
                    capability_id=capability_id,
                    kind=kind,  # type: ignore[arg-type]
                    arguments=arguments,
                    boundary=boundary or Boundary(mode="read_only", allowed_paths=["."]),
                    risk=risk,  # type: ignore[arg-type]
                ),
            },
            inputs=_artifact_ids(inputs),
            outputs=_artifact_ids(outputs),
        )
        return NodeRef(self, id)

    def _ensure_node(self, node_id: str) -> None:
        if node_id not in self._nodes:
            raise ValueError(f"Unknown node '{node_id}'.")


def _artifact_ids(values: list[ArtifactRef | str] | None) -> list[str]:
    return [value.id if isinstance(value, ArtifactRef) else str(value) for value in values or []]


def _node_id(value: NodeRef | str) -> str:
    return value.id if isinstance(value, NodeRef) else str(value)


def _normalize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return {key: _normalize_value(value) for key, value in arguments.items()}


def _normalize_value(value: Any) -> Any:
    if isinstance(value, NodeRef):
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


def _title_from_id(node_id: str) -> str:
    return node_id.replace("_", " ").capitalize()


def _capability_kind_from_id(capability_id: str) -> CapabilityKind:
    kind = capability_id.split(".", 1)[0]
    if kind in {"tool", "mcp", "skill", "shell", "agent", "memory", "file"}:
        return kind  # type: ignore[return-value]
    raise ValueError(f"Cannot infer capability kind from id '{capability_id}'.")
