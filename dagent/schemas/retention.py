"""Provenance of bounded tool output, independent of execution status."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResultStorageWarning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    error_type: str
    message: str


class ResultCursor(BaseModel):
    """A checked tool invocation that continues a file or query window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: Literal["read_file", "grep", "list_files"]
    path: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=1024, ge=1)
    pattern: str | None = None
    glob: str | None = None
    depth: int | None = None


class ResultRetention(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_completeness: Literal["complete", "partial", "unknown"] = "unknown"
    source_reason: str | None = None
    received_bytes: int | None = Field(default=None, ge=0)
    retained_bytes: int | None = Field(default=None, ge=0)
    exit_code: int | None = None
    continuation: ResultCursor | None = None
    window_start: int | None = Field(default=None, ge=0)
    window_length: int | None = Field(default=None, ge=0)
    storage_warnings: tuple[ResultStorageWarning, ...] = ()
    unavailable_fields: tuple[str, ...] = ()
