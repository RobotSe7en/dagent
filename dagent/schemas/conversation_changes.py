"""Opt-in, replayable conversation changes delivered to a host."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from dagent.schemas.conversation import (
    Attachment, CompactionState, ContentReference, ContextSummary, ConversationItem, ModelScope,
)


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


class ConversationToolObservation(_ChangeModel):
    type: Literal["tool_execution"] = "tool_execution"
    call_id: str
    capability_id: str | None = None
    origin: ConversationMessageOrigin
    status: Literal["starting", "completed", "failed", "outcome_unknown"]
    result_item_id: str | None = None


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

    schema_version: Literal[1] = 1
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


class ConversationChangeFailure(_ChangeModel):
    failed_batch: ConversationChangeBatch
    acknowledged_sequence: int = Field(ge=0)
    acknowledged_revision: int | None


ConversationChangeHandler: TypeAlias = Callable[[ConversationChangeBatch], Awaitable[None]]
