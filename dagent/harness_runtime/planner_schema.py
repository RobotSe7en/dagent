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


class PlannerAllValue(_PlannerModel):
    type: Literal["all"]
    values: list["PlannerValue"] = Field(min_length=1)


class PlannerAnyValue(_PlannerModel):
    type: Literal["any"]
    values: list["PlannerValue"] = Field(min_length=1)


class PlannerNotValue(_PlannerModel):
    type: Literal["not"]
    value: "PlannerValue"


PlannerValue: TypeAlias = Annotated[
    PlannerLiteralValue
    | PlannerListValue
    | PlannerObjectValue
    | PlannerGraphInputValue
    | PlannerNodeOutputValue
    | PlannerArtifactValue
    | PlannerFormatValue
    | PlannerCompareValue
    | PlannerAllValue
    | PlannerAnyValue
    | PlannerNotValue,
    Field(discriminator="type"),
]


class PlannerArtifact(_PlannerModel):
    id: PlannerId
    paths: list[str]
    description: str
    required: bool


class PlannerCapabilityNode(_PlannerModel):
    id: PlannerId
    inputs: list[str]
    outputs: list[str]
    type: Literal["capability"]
    capability_id: str
    arguments: list[PlannerNamedValue]

    @model_validator(mode="after")
    def validate_unique_arguments(self) -> "PlannerCapabilityNode":
        _ensure_unique([item.name for item in self.arguments], "Capability argument names")
        return self


class PlannerConditionCase(_PlannerModel):
    branch: str = Field(min_length=1)
    when: PlannerValue

    @model_validator(mode="after")
    def validate_binding_condition(self) -> "PlannerConditionCase":
        if isinstance(
            self.when,
            (PlannerLiteralValue, PlannerListValue, PlannerObjectValue),
        ):
            raise ValueError("Condition case when must be a runtime expression.")
        return self


class PlannerConditionNode(_PlannerModel):
    id: PlannerId
    type: Literal["condition"]
    cases: list[PlannerConditionCase] = Field(min_length=1)
    default_branch: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_branches(self) -> "PlannerConditionNode":
        branches = [case.branch for case in self.cases]
        if any(not branch.strip() for branch in branches):
            raise ValueError("Condition case branches must be non-empty.")
        _ensure_unique(branches, "Condition case branches")
        if not self.default_branch.strip():
            raise ValueError("Condition default_branch must be non-empty.")
        if self.default_branch in branches:
            raise ValueError("Condition default_branch must differ from case branches.")
        return self


PlannerNode: TypeAlias = Annotated[
    PlannerCapabilityNode | PlannerConditionNode,
    Field(discriminator="type"),
]


class PlannerEdge(_PlannerModel):
    source: PlannerId
    target: PlannerId
    when: PlannerValue | None
    branch: str | None

    @model_validator(mode="after")
    def validate_route(self) -> "PlannerEdge":
        if self.when is not None and self.branch is not None:
            raise ValueError("Planner edge cannot declare both when and branch.")
        return self


class PlannerGraph(_PlannerModel):
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


class BuilderPlannerResponse(_PlannerModel):
    """Strict response contract for the SDK-builder planner frontend."""

    action: PlannerAction
    builder_code: str | None
    answer: str | None
    rerun_nodes: list[str]

    @model_validator(mode="after")
    def validate_action_payload(self) -> "BuilderPlannerResponse":
        if self.action == "propose_plan":
            if not str(self.builder_code or "").strip() or self.answer is not None:
                raise ValueError("propose_plan requires builder_code and forbids answer.")
            _ensure_unique(self.rerun_nodes, "rerun_nodes")
            return self
        if self.action == "no_change":
            if self.builder_code is not None or self.answer is not None or self.rerun_nodes:
                raise ValueError("no_change forbids builder_code, answer, and rerun_nodes.")
            return self
        if self.builder_code is not None or self.rerun_nodes:
            raise ValueError("final_answer forbids builder_code and rerun_nodes.")
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


def builder_planner_response_format() -> StructuredOutputFormat:
    """Return the strict response contract for SDK-builder planning."""
    return StructuredOutputFormat(
        name="dagent_dynamic_dag_builder_response",
        description="A DAG Builder proposal, no-change decision, or final answer.",
        schema=_strict_json_schema(BuilderPlannerResponse.model_json_schema()),
        strict=True,
    )


def parse_planner_response(content: str) -> PlannerResponse:
    """Parse provider JSON into the internal planner response model."""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Planner response is not valid JSON: {exc.msg}.") from exc
    return PlannerResponse.model_validate(payload)


def parse_builder_planner_response(content: str) -> BuilderPlannerResponse:
    """Parse provider JSON into the SDK-builder response model."""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Builder planner response is not valid JSON: {exc.msg}.") from exc
    return BuilderPlannerResponse.model_validate(payload)


def _strict_json_schema(value: Any) -> Any:
    """Convert Pydantic output into the strict JSON Schema subset used by providers."""
    if isinstance(value, list):
        return [_strict_json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    output: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"properties", "$defs"}:
            output[key] = {
                name: _strict_json_schema(schema)
                for name, schema in item.items()
            }
        elif key == "oneOf":
            output["anyOf"] = _strict_json_schema(item)
        elif key not in {"default", "discriminator", "title"}:
            output[key] = _strict_json_schema(item)
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
PlannerAllValue.model_rebuild(_types_namespace={"PlannerValue": PlannerValue})
PlannerAnyValue.model_rebuild(_types_namespace={"PlannerValue": PlannerValue})
PlannerNotValue.model_rebuild(_types_namespace={"PlannerValue": PlannerValue})
PlannerGraph.model_rebuild(_types_namespace={"PlannerValue": PlannerValue})
PlannerResponse.model_rebuild(_types_namespace={"PlannerGraph": PlannerGraph})
