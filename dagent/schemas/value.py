"""Structured value expressions for DAG arguments."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field


ValuePathItem: TypeAlias = str | int


class GraphInputExpr(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["graph_input"]
    path: list[ValuePathItem] = Field(default_factory=list)


class NodeOutputExpr(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["node_output"]
    node_id: str
    field: Literal["value", "content", "status", "steps"] = "value"
    path: list[ValuePathItem] = Field(default_factory=list)


class ArtifactExpr(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["artifact"]
    artifact_id: str
    field: Literal["path", "paths", "absolute_path", "absolute_paths"] = "path"


class FormatExpr(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["format"]
    template: str
    values: dict[str, Any] = Field(default_factory=dict)


ValueExpr = Annotated[
    GraphInputExpr | NodeOutputExpr | ArtifactExpr | FormatExpr,
    Field(discriminator="type"),
]


class ValueBinding(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    expr: ValueExpr = Field(alias="$expr")


def bind_value_expr(expr: ValueExpr | dict[str, Any]) -> dict[str, Any]:
    return ValueBinding(expr=expr).model_dump(mode="json", by_alias=True)


def parse_value_binding(value: Any) -> ValueExpr | None:
    if not isinstance(value, dict) or set(value) != {"$expr"}:
        return None
    return ValueBinding.model_validate(value).expr


def iter_value_exprs(value: Any):
    expr = parse_value_binding(value)
    if expr is not None:
        yield expr
        if isinstance(expr, FormatExpr):
            for item in expr.values.values():
                yield from iter_value_exprs(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from iter_value_exprs(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_value_exprs(item)


def iter_node_output_exprs(value: Any):
    for expr in iter_value_exprs(value):
        if isinstance(expr, NodeOutputExpr):
            yield expr
