"""Structured value expressions for DAG arguments."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError


ValuePathItem: TypeAlias = str | int


class ValueExpressionError(ValueError):
    """Raised when a value expression envelope is malformed."""


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


CompareOp = Literal["eq", "ne", "gt", "ge", "lt", "le"]


class CompareExpr(BaseModel):
    """Compare two values after resolving nested DAG value references."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["compare"]
    op: CompareOp
    left: Any
    right: Any


class ItemExpr(BaseModel):
    """The current map item or loop body output, valid only in those scopes."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["item"]
    path: list[ValuePathItem] = Field(default_factory=list)


ValueExpr = Annotated[
    GraphInputExpr | NodeOutputExpr | ArtifactExpr | FormatExpr | CompareExpr | ItemExpr,
    Field(discriminator="type"),
]


class ValueBinding(BaseModel):
    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True, extra="forbid")

    expr: ValueExpr = Field(alias="$expr")


def bind_value_expr(expr: ValueExpr | dict[str, Any]) -> dict[str, Any]:
    return ValueBinding(expr=expr).model_dump(mode="json", by_alias=True)


def parse_value_binding(value: Any) -> ValueExpr | None:
    if isinstance(value, ValueBinding):
        return value.expr
    if not isinstance(value, dict):
        return None
    if "$expr" not in value:
        return None
    if set(value) != {"$expr"}:
        raise ValueExpressionError("value expression envelope must contain only '$expr'.")
    try:
        return ValueBinding.model_validate(value).expr
    except ValidationError as exc:
        raise ValueExpressionError(str(exc)) from exc


def iter_value_exprs(value: Any):
    expr = parse_value_binding(value)
    if expr is not None:
        yield expr
        if isinstance(expr, FormatExpr):
            for item in expr.values.values():
                yield from iter_value_exprs(item)
        elif isinstance(expr, CompareExpr):
            yield from iter_value_exprs(expr.left)
            yield from iter_value_exprs(expr.right)
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


def iter_artifact_exprs(value: Any):
    for expr in iter_value_exprs(value):
        if isinstance(expr, ArtifactExpr):
            yield expr


def has_item_expr(value: Any) -> bool:
    return any(isinstance(expr, ItemExpr) for expr in iter_value_exprs(value))
