"""User-facing DAG builder and static DAG runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True)
class ArtifactRef:
    """Reference to a DAG artifact."""

    id: str

    @property
    def path(self) -> str:
        return f"{{{{artifact.{self.id}.path}}}}"


class NodeRef:
    """Reference to a node inside a user-built DAG."""

    def __init__(self, dag: "Dag", node_id: str) -> None:
        self._dag = dag
        self.id = node_id

    @property
    def output(self) -> str:
        return f"{{{{{self.id}.output}}}}"

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
    ) -> None:
        self.id = id
        self.name = name or id
        self.description = description
        self.version = version
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
        title: str | None = None,
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
            title=title,
            inputs=inputs,
            outputs=outputs,
            boundary=boundary,
        )

    def agent_node(
        self,
        id: str,
        agent: ToolAgent,
        *,
        prompt: str,
        max_steps: int = 8,
        title: str | None = None,
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
        self._agents[agent_name] = agent
        return self._add_capability_node(
            id,
            capability_id=capability_id,
            kind="agent",
            risk="medium",
            arguments={"prompt": prompt, "max_steps": max_steps},
            title=title,
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
    normalized = {}
    for key, value in arguments.items():
        if isinstance(value, NodeRef):
            normalized[key] = value.output
        elif isinstance(value, ArtifactRef):
            normalized[key] = value.path
        else:
            normalized[key] = value
    return normalized


def _title_from_id(node_id: str) -> str:
    return node_id.replace("_", " ").capitalize()


def _capability_kind_from_id(capability_id: str) -> CapabilityKind:
    kind = capability_id.split(".", 1)[0]
    if kind in {"tool", "mcp", "skill", "shell", "custom_tool", "agent", "memory", "file"}:
        return kind  # type: ignore[return-value]
    raise ValueError(f"Cannot infer capability kind from id '{capability_id}'.")
