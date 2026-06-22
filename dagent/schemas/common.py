"""Shared low-level schema primitives."""

from __future__ import annotations

import inspect
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PydanticSchemaGenerationError, PydanticUserError, TypeAdapter

from dagent.schemas.value import ValueBinding


RiskLevel = Literal["low", "medium", "high"]
BoundaryValue = str | ValueBinding


class Boundary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_paths: list[BoundaryValue] = Field(default_factory=list)

    def policy_decision(self) -> dict[str, Any]:
        return {
            "allowed_paths": list(self.allowed_paths),
        }


def json_schema_for_type(annotation: Any) -> dict[str, Any]:
    if annotation is None or annotation is inspect.Signature.empty:
        return {}
    try:
        schema = TypeAdapter(annotation).json_schema()
    except (PydanticSchemaGenerationError, PydanticUserError, TypeError, ValueError):
        return {"type": "object"}
    return dict(schema)
