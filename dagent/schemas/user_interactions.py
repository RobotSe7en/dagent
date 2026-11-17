"""Versioned, transport-neutral user interaction records."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator


class _InteractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UserInteractionOption(_InteractionModel):
    id: StrictStr = Field(min_length=1)
    label: StrictStr


class ConfirmParameters(_InteractionModel):
    title: StrictStr
    description: StrictStr | None = None


class SelectParameters(_InteractionModel):
    title: StrictStr
    options: tuple[UserInteractionOption, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_options(self) -> SelectParameters:
        if len({option.id for option in self.options}) != len(self.options):
            raise ValueError("Interaction option IDs must be unique.")
        return self


class InputParameters(_InteractionModel):
    title: StrictStr
    prompt: StrictStr = ""
    initial: StrictStr = ""


class EditorParameters(_InteractionModel):
    title: StrictStr
    content: StrictStr
    format: Literal["text", "markdown", "html"] = "text"


class _RequestIdentity(_InteractionModel):
    protocol_version: Literal[1] = 1
    interaction_id: StrictStr
    run_id: StrictStr
    invocation_id: StrictStr
    assistant_message_id: StrictStr
    capability_id: StrictStr


class ConfirmRequest(_RequestIdentity):
    type: Literal["confirm"] = "confirm"
    parameters: ConfirmParameters


class SelectRequest(_RequestIdentity):
    type: Literal["select"] = "select"
    parameters: SelectParameters


class InputRequest(_RequestIdentity):
    type: Literal["input"] = "input"
    parameters: InputParameters


class EditorRequest(_RequestIdentity):
    type: Literal["editor"] = "editor"
    parameters: EditorParameters


UserInteractionRequest = Annotated[
    ConfirmRequest | SelectRequest | InputRequest | EditorRequest,
    Field(discriminator="type"),
]


class _ResponseIdentity(_InteractionModel):
    protocol_version: Literal[1] = 1
    interaction_id: StrictStr
    run_id: StrictStr
    invocation_id: StrictStr


class UserInteractionAnswer(_ResponseIdentity):
    outcome: Literal["submitted"] = "submitted"
    value: StrictBool | StrictStr


class UserInteractionAbandonment(_ResponseIdentity):
    outcome: Literal["cancelled"] = "cancelled"


UserInteractionResponse = Annotated[
    UserInteractionAnswer | UserInteractionAbandonment,
    Field(discriminator="outcome"),
]

InteractionEndReason = Literal[
    "submitted", "cancelled", "timeout", "unavailable", "run_cancelled",
    "runner_closed", "invocation_ended", "delivery_error",
]


class UserInteractionEnded(_InteractionModel):
    protocol_version: Literal[1] = 1
    interaction_id: StrictStr
    run_id: StrictStr
    invocation_id: StrictStr
    assistant_message_id: StrictStr
    capability_id: StrictStr
    reason: InteractionEndReason
    invalidated: bool
