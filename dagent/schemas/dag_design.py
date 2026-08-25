"""Public contracts for non-executing DAG design."""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dagent.schemas.context import ContextUsage, ModelTokenUsage
from dagent.schemas.conversation import ConversationState
from dagent.schemas.dag import DAGSpec


DAGDiagnosticSeverity = Literal["info", "warning", "error"]


class DAGDiagnostic(BaseModel):
    """One deterministic or model-boundary DAG diagnostic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: DAGDiagnosticSeverity
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    node_id: str | None = None
    path: tuple[str | int, ...] = ()


class DAGDesignSelection(BaseModel):
    """Optional UI-neutral hint identifying the focus of a design turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_unique_node_ids(self) -> "DAGDesignSelection":
        if len(set(self.node_ids)) != len(self.node_ids):
            raise ValueError("Selected node ids must be unique.")
        return self


class _DAGDesignResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    diagnostics: tuple[DAGDiagnostic, ...] = ()
    conversation: ConversationState
    usage: ModelTokenUsage | None = None
    context_usage: ContextUsage | None = None


class DAGDesignProposal(_DAGDesignResult):
    """A complete, validated candidate DAG design."""

    type: Literal["proposal"] = "proposal"
    candidate: DAGSpec
    summary: str = Field(min_length=1)


class DAGDesignNoChange(_DAGDesignResult):
    """The current DAG already satisfies the instruction."""

    type: Literal["no_change"] = "no_change"
    summary: str = Field(min_length=1)


class DAGDesignAnswer(_DAGDesignResult):
    """An answer or explanation that does not propose a DAG change."""

    type: Literal["answer"] = "answer"
    answer: str = Field(min_length=1)


class DAGDesignFailure(_DAGDesignResult):
    """A typed failure at the model, catalog, or DAG validation boundary."""

    type: Literal["failure"] = "failure"
    diagnostics: tuple[DAGDiagnostic, ...] = Field(min_length=1)


DAGDesignResult: TypeAlias = Annotated[
    DAGDesignProposal | DAGDesignNoChange | DAGDesignAnswer | DAGDesignFailure,
    Field(discriminator="type"),
]


__all__ = [
    "DAGDesignAnswer",
    "DAGDesignFailure",
    "DAGDesignNoChange",
    "DAGDesignProposal",
    "DAGDesignResult",
    "DAGDesignSelection",
    "DAGDiagnostic",
    "DAGDiagnosticSeverity",
]
