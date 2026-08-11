"""Structured value expressions for DAG arguments."""

from __future__ import annotations

from string import Formatter
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


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

    @model_validator(mode="after")
    def validate_template_values(self) -> "FormatExpr":
        try:
            fields = _format_field_roots(self.template)
        except ValueError as exc:
            raise ValueError(f"Invalid format template: {exc}") from exc
        missing = sorted(field for field in fields if field not in self.values)
        if missing:
            raise ValueError(
                "Format template references missing values: " + ", ".join(missing)
            )
        return self


CompareOp = Literal["eq", "ne", "gt", "ge", "lt", "le"]


class CompareExpr(BaseModel):
    """Compare two values after resolving nested DAG value references."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["compare"]
    op: CompareOp
    left: Any
    right: Any


class AllExpr(BaseModel):
    """Resolve values in order and require every value to be truthy."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["all"]
    values: list[Any] = Field(min_length=1)


class AnyExpr(BaseModel):
    """Resolve values in order and require at least one truthy value."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["any"]
    values: list[Any] = Field(min_length=1)


class NotExpr(BaseModel):
    """Negate the truthiness of one resolved value."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["not"]
    value: Any


class ItemExpr(BaseModel):
    """The current map item or loop body output, valid only in those scopes."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["item"]
    path: list[ValuePathItem] = Field(default_factory=list)


ValueExpr = Annotated[
    GraphInputExpr
    | NodeOutputExpr
    | ArtifactExpr
    | FormatExpr
    | CompareExpr
    | AllExpr
    | AnyExpr
    | NotExpr
    | ItemExpr,
    Field(discriminator="type"),
]


def _format_field_roots(template: str) -> set[str]:
    fields: set[str] = set()
    for _, field_name, format_spec, _ in Formatter().parse(template):
        if field_name is not None:
            root = field_name
            for marker in (".", "["):
                marker_index = root.find(marker)
                if marker_index != -1:
                    root = root[:marker_index]
            if not root or root.isdecimal():
                raise ValueError("format fields must use named values")
            fields.add(root)
        if format_spec:
            fields.update(_format_field_roots(format_spec))
    return fields


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
        elif isinstance(expr, (AllExpr, AnyExpr)):
            for item in expr.values:
                yield from iter_value_exprs(item)
        elif isinstance(expr, NotExpr):
            yield from iter_value_exprs(expr.value)
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from iter_value_exprs(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_value_exprs(item)


def iter_artifact_exprs(value: Any):
    for expr in iter_value_exprs(value):
        if isinstance(expr, ArtifactExpr):
            yield expr
