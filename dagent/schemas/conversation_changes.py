"""Opt-in, replayable conversation changes delivered to a host."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from dagent.schemas.conversation import (
    Attachment, CompactionState, ContentReference, ContextSummary, ConversationItem, ModelScope,
)
from dagent.schemas.context import ContextUsage
from dagent.schemas.results import ExecutionUsage, RunCheckpoint
from dagent.schemas.retention import ResultRetention
from dagent.schemas.run_trace import RunTraceError as RunError


class _ChangeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConversationMessageOrigin(_ChangeModel):
    id: str
    scope: ModelScope = "conversation"


class ConversationMessageChange(_ChangeModel):
    item: ConversationItem
    origin: ConversationMessageOrigin
    response_id: str | None = None
    partial: bool = False


class ConversationItemPlacement(_ChangeModel):
    item_id: str
    index: int = Field(ge=0)


class ConversationSummaryReplacement(_ChangeModel):
    value: ContextSummary | None
    replaced_item_ids: tuple[str, ...] = ()


class ConversationCompactionStateReplacement(_ChangeModel):
    value: CompactionState | None


class ConversationContextChange(_ChangeModel):
    added: tuple[ConversationItemPlacement, ...] = ()
    updated_item_ids: tuple[str, ...] = ()
    deactivated_item_ids: tuple[str, ...] = ()
    summary_change: ConversationSummaryReplacement | None = None
    compaction_state_change: ConversationCompactionStateReplacement | None = None


class ConversationResponseObservation(_ChangeModel):
    type: Literal["response"] = "response"
    message_id: str
    response_id: str
    origin: ConversationMessageOrigin
    status: Literal["started", "completed", "failed", "cancelled"]
    observed_content: str | None = None
    observed_reasoning: str | None = None
    error: RunError | None = None

    @model_serializer(mode="wrap")
    def serialize_error(self, serializer):
        payload = serializer(self)
        if self.error is None:
            payload.pop("error", None)
        return payload


class ConversationToolObservation(_ChangeModel):
    type: Literal["tool_execution"] = "tool_execution"
    call_id: str
    capability_id: str | None = None
    origin: ConversationMessageOrigin
    status: Literal["starting", "completed", "failed", "outcome_unknown"]
    result_item_id: str | None = None
    assistant_message_id: str | None = None
    error: RunError | None = None
    retention: ResultRetention | None = None

    @model_serializer(mode="wrap")
    def serialize_details(self, serializer):
        payload = serializer(self)
        for name in ("assistant_message_id", "error", "retention"):
            if getattr(self, name) is None:
                payload.pop(name, None)
        return payload


ConversationObservation: TypeAlias = Annotated[
    ConversationResponseObservation | ConversationToolObservation,
    Field(discriminator="type"),
]


class ConversationResourceReady(_ChangeModel):
    reference: ContentReference | Attachment
    workspace_path: str
    runtime_directory: str


class ConversationChangeBatch(_ChangeModel):
    """Apply message records, then context changes, atomically in batch order.

    Message keys are (origin.id, item.id). Public context placements use origin
    id equal to conversation_id. Internal audit records never change membership.
    """

    schema_version: Literal[1, 2] = 1
    conversation_schema_version: Literal[4] | None
    batch_id: str
    delivery_id: str
    sequence: int = Field(ge=1)
    conversation_id: str | None
    run_id: str
    base_revision: int | None
    revision: int | None
    messages: tuple[ConversationMessageChange, ...] = ()
    context: ConversationContextChange | None = None
    observations: tuple[ConversationObservation, ...] = ()
    resources: tuple[ConversationResourceReady, ...] = ()
    status: Literal["completed", "awaiting_review", "failed", "cancelled"] | None = None
    usage: ExecutionUsage | None = None
    context_usage: tuple[ContextUsage, ...] | None = None
    error: RunError | None = None
    workspace_path: str | None = None
    output_item_id: str | None = None
    output_text: str | None = None
    checkpoint: RunCheckpoint | None = None

    @model_validator(mode="after")
    def validate_delivery(self):
        if self.schema_version == 1 and any(value is not None for value in (
            self.status, self.usage, self.context_usage, self.error, self.workspace_path,
            self.output_item_id, self.output_text, self.checkpoint,
        )):
            raise ValueError("Delivery metadata requires batch schema_version=2.")
        if self.schema_version == 2:
            if self.status is None and any(value is not None for value in (
                self.usage, self.context_usage, self.error, self.output_item_id, self.output_text, self.checkpoint,
            )):
                raise ValueError("Terminal fields require a terminal status.")
            if self.status is not None and self.usage is None:
                raise ValueError("Terminal batches require cumulative usage.")
            if self.status == "failed" and self.error is None:
                raise ValueError("Failed terminal batches require an error.")
            if (self.status == "awaiting_review") != (self.checkpoint is not None):
                raise ValueError("Only awaiting_review batches require a checkpoint.")
            if self.checkpoint is not None and (
                self.checkpoint.state.kind != "tool"
                or self.checkpoint.state.status != "awaiting_review"
                or self.checkpoint.state.run_id != self.run_id
                or self.checkpoint.state.conversation is None
                or self.checkpoint.state.conversation.id != self.conversation_id
                or self.checkpoint.state.conversation.revision != self.revision
                or self.checkpoint.state.trace is not None
                or self.checkpoint.usage != self.usage
            ):
                raise ValueError("Checkpoint does not match this tool delivery.")
            if any(isinstance(o, ConversationToolObservation) and not o.assistant_message_id
                   for o in self.observations):
                raise ValueError("Tool observations require assistant_message_id.")
        if self.output_item_id is not None and self.output_text is not None:
            raise ValueError("Output must use either a message reference or text.")
        return self

    @model_serializer(mode="wrap")
    def serialize_terminal(self, serializer):
        payload = serializer(self)
        for name in ("status", "usage", "context_usage", "error", "workspace_path",
                     "output_item_id", "output_text", "checkpoint"):
            if getattr(self, name) is None:
                payload.pop(name, None)
        return payload
