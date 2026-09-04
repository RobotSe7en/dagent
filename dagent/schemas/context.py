"""Context-window, compaction, and normalized-result policies."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


ReasoningReplayMode: TypeAlias = Literal["none", "active_run", "all_runs"]


class ContextPolicy(BaseModel):
    """Immutable policy for model-context assembly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reasoning_replay: ReasoningReplayMode = "active_run"
    compaction_trigger_ratio: float = Field(default=0.8, gt=0, le=1)
    summary_max_tokens: int = Field(default=1024, ge=64)
    max_tool_result_tokens: int = Field(default=2048, ge=64)
    max_total_tool_result_tokens: int = Field(default=8192, ge=64)
    token_safety_margin: float = Field(default=0.15, ge=0, le=1)

    @model_validator(mode="after")
    def validate_tool_budgets(self) -> "ContextPolicy":
        if self.max_tool_result_tokens > self.max_total_tool_result_tokens:
            raise ValueError(
                "max_tool_result_tokens cannot exceed max_total_tool_result_tokens."
            )
        return self


class ResultStoragePolicy(BaseModel):
    """Run-workspace storage policy for large capability results."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_inline_bytes: int = Field(default=256 * 1024, ge=1024)


class ContextUsage(BaseModel):
    """Observable token estimate and reductions for one model request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_window_tokens: int = Field(ge=1)
    output_reserve_tokens: int = Field(ge=0)
    input_budget_tokens: int = Field(ge=1)
    estimated_input_tokens: int = Field(ge=0)
    system_tokens: int = Field(default=0, ge=0)
    schema_tokens: int = Field(default=0, ge=0)
    summary_tokens: int = Field(default=0, ge=0)
    history_tokens: int = Field(default=0, ge=0)
    tool_result_tokens: int = Field(default=0, ge=0)
    included_items: int = Field(default=0, ge=0)
    compacted_items: int = Field(default=0, ge=0)
    truncated_tool_results: int = Field(default=0, ge=0)
    estimator: Literal["heuristic", "custom", "vllm"] = "heuristic"
    server_max_model_len: int | None = Field(default=None, ge=1)
    configured_context_limit: int | None = Field(default=None, ge=1)
    reasoning_replay_mode: ReasoningReplayMode = "active_run"
    replayed_reasoning_items: int = Field(default=0, ge=0)
    replayed_reasoning_tokens: int = Field(default=0, ge=0)
    omitted_reasoning_items: int = Field(default=0, ge=0)
    omitted_reasoning_tokens: int = Field(default=0, ge=0)
    compacted_active_run_items: int = Field(default=0, ge=0)
    compaction_method: Literal["none", "model", "deterministic_fallback"] = "none"
    compaction_reason: str | None = None


class ModelTokenUsage(BaseModel):
    """Provider-reported token usage when an endpoint supplies it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> "ModelTokenUsage":
        minimum = self.input_tokens + self.output_tokens
        if self.total_tokens < minimum:
            raise ValueError("total_tokens cannot be less than input_tokens + output_tokens.")
        return self


class ModelCallMetadata(BaseModel):
    """Resolved protocol and reasoning controls for one provider call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal["chat_completions", "responses"]
    requested_reasoning_effort: str | None = None
    effective_reasoning_effort: str | None = None
    requested_budget_tokens: int | None = Field(default=None, ge=1)
    effective_budget_tokens: int | None = Field(default=None, ge=1)
    ignored_parameters: tuple[str, ...] = ()
    fallback_reason: str | None = None


class ContextWindowExceeded(RuntimeError):
    """Raised before provider invocation when mandatory input cannot fit."""

    def __init__(self, message: str, *, usage: ContextUsage) -> None:
        self.usage = usage
        super().__init__(message)


__all__ = [
    "ContextPolicy",
    "ContextUsage",
    "ContextWindowExceeded",
    "ModelCallMetadata",
    "ModelTokenUsage",
    "ReasoningReplayMode",
    "ResultStoragePolicy",
]
