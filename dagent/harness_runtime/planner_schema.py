"""Internal structured-output contract for dynamic DAG planning."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dagent.providers import StructuredOutputFormat


class _PlannerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


PlannerId: TypeAlias = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]


class PlannerLiteralValue(_PlannerModel):
    type: Literal["literal"]
    value: str | int | float | bool | None


class PlannerListValue(_PlannerModel):
    type: Literal["list"]
    items: list["PlannerValue"]


class PlannerNamedValue(_PlannerModel):
    name: str
    value: "PlannerValue"


class PlannerObjectValue(_PlannerModel):
    type: Literal["object"]
    entries: list[PlannerNamedValue]

    @model_validator(mode="after")
    def validate_unique_names(self) -> "PlannerObjectValue":
        _ensure_unique([entry.name for entry in self.entries], "Object entry names")
        return self


class PlannerGraphInputValue(_PlannerModel):
    type: Literal["graph_input"]
    path: list[str | int]


class PlannerNodeOutputValue(_PlannerModel):
    type: Literal["node_output"]
    node_id: PlannerId
    field: Literal["value", "content", "status", "steps"]
    path: list[str | int]


class PlannerArtifactValue(_PlannerModel):
    type: Literal["artifact"]
    artifact_id: PlannerId
    field: Literal["path", "paths", "absolute_path", "absolute_paths"]


class PlannerFormatValue(_PlannerModel):
    type: Literal["format"]
    template: str
    values: list[PlannerNamedValue]

    @model_validator(mode="after")
    def validate_unique_names(self) -> "PlannerFormatValue":
        _ensure_unique([item.name for item in self.values], "Format value names")
        return self


class PlannerCompareValue(_PlannerModel):
    type: Literal["compare"]
    op: Literal["eq", "ne", "gt", "ge", "lt", "le"]
    left: "PlannerValue"
    right: "PlannerValue"


class PlannerItemValue(_PlannerModel):
    type: Literal["item"]
    path: list[str | int]


PlannerValue: TypeAlias = Annotated[
    PlannerLiteralValue
    | PlannerListValue
    | PlannerObjectValue
    | PlannerGraphInputValue
    | PlannerNodeOutputValue
    | PlannerArtifactValue
    | PlannerFormatValue
    | PlannerCompareValue
    | PlannerItemValue,
    Field(discriminator="type"),
]


class PlannerArtifact(_PlannerModel):
    id: PlannerId
    paths: list[str]
    description: str
    required: bool


class _PlannerNode(_PlannerModel):
    id: PlannerId
    title: str
    inputs: list[str]
    outputs: list[str]


class PlannerCapabilityNode(_PlannerNode):
    type: Literal["capability"]
    capability_id: str
    arguments: list[PlannerNamedValue]

    @model_validator(mode="after")
    def validate_unique_arguments(self) -> "PlannerCapabilityNode":
        _ensure_unique([item.name for item in self.arguments], "Capability argument names")
        return self


class PlannerMapNode(_PlannerNode):
    type: Literal["map"]
    items: PlannerValue
    capability_id: str
    arguments: list[PlannerNamedValue]
    max_items: int = Field(ge=1)
    max_concurrency: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_unique_arguments(self) -> "PlannerMapNode":
        _ensure_unique([item.name for item in self.arguments], "Map argument names")
        return self


class PlannerSubgraphNode(_PlannerNode):
    type: Literal["subgraph"]
    graph: "PlannerGraph"
    input: PlannerValue | None


class PlannerLoopNode(_PlannerNode):
    type: Literal["loop"]
    body: "PlannerGraph"
    until: PlannerValue
    max_iterations: int = Field(ge=1)
    input: PlannerValue | None


PlannerNode: TypeAlias = Annotated[
    PlannerCapabilityNode | PlannerMapNode | PlannerSubgraphNode | PlannerLoopNode,
    Field(discriminator="type"),
]


class PlannerEdge(_PlannerModel):
    source: PlannerId
    target: PlannerId
    reason: str
    when: PlannerValue | None


class PlannerGraph(_PlannerModel):
    name: str
    description: str
    artifacts: list[PlannerArtifact]
    nodes: list[PlannerNode]
    edges: list[PlannerEdge]
    output: PlannerValue | None

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "PlannerGraph":
        _ensure_unique([artifact.id for artifact in self.artifacts], "Artifact ids")
        _ensure_unique([node.id for node in self.nodes], "Node ids")
        if any(node.id == "start" for node in self.nodes):
            raise ValueError("Node id 'start' is reserved for runtime bookkeeping.")
        return self


PlannerAction = Literal["propose_plan", "no_change", "final_answer"]


class PlannerResponse(_PlannerModel):
    action: PlannerAction
    plan: PlannerGraph | None
    answer: str | None
    rerun_nodes: list[str]

    @model_validator(mode="after")
    def validate_action_payload(self) -> "PlannerResponse":
        if self.action == "propose_plan":
            if self.plan is None or self.answer is not None:
                raise ValueError("propose_plan requires plan and forbids answer.")
            _ensure_unique(self.rerun_nodes, "rerun_nodes")
            return self
        if self.action == "no_change":
            if self.plan is not None or self.answer is not None or self.rerun_nodes:
                raise ValueError("no_change forbids plan, answer, and rerun_nodes.")
            return self
        if self.plan is not None or self.rerun_nodes:
            raise ValueError("final_answer forbids plan and rerun_nodes.")
        if not str(self.answer or "").strip():
            raise ValueError("final_answer requires a non-empty answer.")
        return self


def planner_response_format() -> StructuredOutputFormat:
    """Return the strict provider response contract for a planner turn."""
    return StructuredOutputFormat(
        name="dagent_dynamic_dag_response",
        description="A typed dynamic DAG proposal, no-change decision, or final answer.",
        schema=_strict_json_schema(PlannerResponse.model_json_schema()),
        strict=True,
    )


def parse_planner_response(content: str) -> PlannerResponse:
    """Parse provider JSON into the internal planner response model."""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Planner response is not valid JSON: {exc.msg}.") from exc
    return PlannerResponse.model_validate(payload)


def _strict_json_schema(value: Any) -> Any:
    """Convert Pydantic output into the strict JSON Schema subset used by providers."""
    if isinstance(value, list):
        return [_strict_json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    output = {
        key: _strict_json_schema(item)
        for key, item in value.items()
        if key not in {"default", "title"}
    }
    properties = output.get("properties")
    if isinstance(properties, dict):
        output["required"] = list(properties)
        output["additionalProperties"] = False
    return output


def _ensure_unique(values: list[str], label: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        raise ValueError(f"{label} must be unique: {', '.join(sorted(duplicates))}.")


PlannerListValue.model_rebuild(_types_namespace={"PlannerValue": PlannerValue})
PlannerNamedValue.model_rebuild(_types_namespace={"PlannerValue": PlannerValue})
PlannerObjectValue.model_rebuild(_types_namespace={"PlannerValue": PlannerValue})
PlannerFormatValue.model_rebuild(_types_namespace={"PlannerValue": PlannerValue})
PlannerCompareValue.model_rebuild(_types_namespace={"PlannerValue": PlannerValue})
PlannerSubgraphNode.model_rebuild(
    _types_namespace={"PlannerGraph": PlannerGraph, "PlannerValue": PlannerValue}
)
PlannerLoopNode.model_rebuild(
    _types_namespace={"PlannerGraph": PlannerGraph, "PlannerValue": PlannerValue}
)
PlannerGraph.model_rebuild(
    _types_namespace={"PlannerNode": PlannerNode, "PlannerValue": PlannerValue}
)
PlannerResponse.model_rebuild(_types_namespace={"PlannerGraph": PlannerGraph})
