"""Shared low-level schema primitives."""

from __future__ import annotations

import inspect
from typing import Any, Literal

from pydantic import BaseModel, Field, PydanticSchemaGenerationError, TypeAdapter


BoundaryMode = Literal["read_only", "write_limited", "full"]
RiskLevel = Literal["low", "medium", "high"]


class Boundary(BaseModel):
    mode: BoundaryMode = "read_only"
    allowed_paths: list[Any] = Field(default_factory=list)
    allowed_commands: list[Any] = Field(default_factory=list)


def json_schema_for_type(annotation: Any) -> dict[str, Any]:
    if annotation is None or annotation is inspect.Signature.empty:
        return {}
    try:
        schema = TypeAdapter(annotation).json_schema()
    except (PydanticSchemaGenerationError, TypeError, ValueError):
        return {"type": "object"}
    return dict(schema)
