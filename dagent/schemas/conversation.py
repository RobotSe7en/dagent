"""Provider-neutral conversation and model-thread contracts."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal, TypeAlias
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dagent.schemas.context import ContextUsage, ModelCallMetadata, ModelTokenUsage


ModelScope = Literal["conversation", "router", "planner", "validator", "subagent", "compactor"]
ItemVisibility = Literal["user", "internal"]
ToolResultStatus = Literal[
    "completed",
    "failed",
    "pending_review",
    "denied",
    "skipped",
]


class InlineContent(BaseModel):
    """Small text content kept inline in a result or checkpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["inline"] = "inline"
    text: str = ""


class ContentReference(BaseModel):
    """Workspace-relative reference to normalized run output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["dagent_content_reference"] = "dagent_content_reference"
    path: str = Field(min_length=1)
    media_type: str = "application/octet-stream"
    byte_length: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preview: str = ""

    @model_validator(mode="after")
    def validate_path(self) -> "ContentReference":
        _validate_relative_path(self.path, label="Content reference")
        return self


StoredContent: TypeAlias = Annotated[
    InlineContent | ContentReference,
    Field(discriminator="type"),
]


class ToolCallItem(BaseModel):
    """Provider-neutral structured tool call emitted by an assistant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    capability_id: str | None = None


class Attachment(BaseModel):
    """A user-supplied file materialized inside a run workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["file"] = "file"
    id: str = Field(default_factory=lambda: f"attachment_{uuid4().hex}")
    path: str = Field(min_length=1)
    media_type: str = "application/octet-stream"
    byte_length: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_path(self) -> "Attachment":
        _validate_relative_path(self.path, label="Attachment")
        return self


class UserMessage(BaseModel):
    """One user or runtime-authored input message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["user"] = "user"
    id: str = Field(default_factory=lambda: f"msg_{uuid4().hex}")
    run_id: str | None = None
    content: str
    attachments: tuple[Attachment, ...] = ()
    scope: ModelScope = "conversation"
    visibility: ItemVisibility = "user"


class AssistantMessage(BaseModel):
    """One model response, including auditable, policy-controlled reasoning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["assistant"] = "assistant"
    id: str = Field(default_factory=lambda: f"msg_{uuid4().hex}")
    run_id: str | None = None
    content: str = ""
    reasoning: str = ""
    refusal: str = ""
    usage: ModelTokenUsage | None = None
    model_call: ModelCallMetadata | None = None
    tool_calls: tuple[ToolCallItem, ...] = ()
    scope: ModelScope = "conversation"
    visibility: ItemVisibility = "user"


class ToolResultMessage(BaseModel):
    """Recorded result for one assistant tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["tool_result"] = "tool_result"
    id: str = Field(default_factory=lambda: f"msg_{uuid4().hex}")
    run_id: str | None = None
    call_id: str
    name: str
    capability_id: str | None = None
    status: ToolResultStatus
    content: StoredContent = Field(default_factory=InlineContent)
    value: Any = None
    value_reference: ContentReference | None = None
    artifacts: tuple[ContentReference, ...] = ()
    scope: ModelScope = "conversation"
    visibility: ItemVisibility = "internal"


ConversationItem: TypeAlias = Annotated[
    UserMessage | AssistantMessage | ToolResultMessage,
    Field(discriminator="type"),
]


class ContextSummary(BaseModel):
    """Incremental semantic summary replacing compacted conversation turns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    source_item_count: int = Field(ge=1)
    method: Literal["model", "deterministic_fallback"] = "model"
    fallback_reason: str | None = None
    source_truncated: bool = False
    output_truncated: bool = False
    reasoning: str = ""
    usage: ModelTokenUsage | None = None
    model_call: ModelCallMetadata | None = None
    context_usage: ContextUsage | None = None


class ConversationState(BaseModel):
    """Portable, bounded state used to continue a conversation across Runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[4] = 4
    id: str = Field(default_factory=lambda: f"conversation_{uuid4().hex}")
    revision: int = Field(default=0, ge=0)
    summary: ContextSummary | None = None
    items: tuple[ConversationItem, ...] = ()

    @model_validator(mode="after")
    def validate_item_ids(self) -> "ConversationState":
        item_ids = [item.id for item in self.items]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("Conversation item ids must be unique.")
        return self


def inline_content(value: str) -> InlineContent:
    return InlineContent(text=value)


def stored_content_text(value: StoredContent) -> str:
    if isinstance(value, InlineContent):
        return value.text
    return value.preview


def _validate_relative_path(value: str, *, label: str) -> None:
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in posix.parts)
        or any(part in {"", ".", ".."} for part in windows.parts)
    ):
        raise ValueError(f"{label} path must be workspace-relative and traversal-free.")


__all__ = [
    "AssistantMessage",
    "Attachment",
    "ContentReference",
    "ContextSummary",
    "ConversationItem",
    "ConversationState",
    "InlineContent",
    "ItemVisibility",
    "ModelScope",
    "StoredContent",
    "ToolCallItem",
    "ToolResultMessage",
    "ToolResultStatus",
    "UserMessage",
    "inline_content",
    "stored_content_text",
]
