"""Shared low-level schema primitives."""

from __future__ import annotations

import inspect
from pathlib import PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PydanticSchemaGenerationError, PydanticUserError, TypeAdapter

from dagent.schemas.value import ValueBinding


RiskLevel = Literal["low", "medium", "high"]
BoundaryValue = str | ValueBinding
MAX_EXTRA_SYSTEM_PROMPT_LENGTH = 16_384


def validate_extra_system_prompt(value: Any) -> str | None:
    """Validate the optional runner-level system-prompt addition."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("extra_system_prompt must be None or a string.")
    if not value.strip():
        raise ValueError("extra_system_prompt must be a non-empty string.")
    if len(value) > MAX_EXTRA_SYSTEM_PROMPT_LENGTH:
        raise ValueError(
            "extra_system_prompt must be at most "
            f"{MAX_EXTRA_SYSTEM_PROMPT_LENGTH} characters."
        )
    return value


def validate_runtime_directory(value: Any) -> str:
    """Return a canonical safe relative runtime directory."""

    if not isinstance(value, (str, PurePath)):
        raise ValueError(
            "runtime_directory must be a safe, non-empty relative path."
        )
    raw = str(value)
    posix = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    segments = raw.replace("\\", "/").split("/")
    if (
        not raw
        or not posix.parts
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in segments)
        or any(part in {"", ".", ".."} for part in posix.parts)
        or any(part in {"", ".", ".."} for part in windows.parts)
    ):
        raise ValueError(
            "runtime_directory must be a safe, non-empty relative path."
        )
    return windows.as_posix() if "\\" in raw else posix.as_posix()


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
