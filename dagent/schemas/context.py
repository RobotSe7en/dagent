"""Context-window, compaction, and normalized-result policies."""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


DEFAULT_CONTEXT_WINDOW_TOKENS = 131072


ReasoningReplayMode: TypeAlias = Literal["none", "active_run", "all_runs"]
ReasoningEffort: TypeAlias = Literal[
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
]


class ContextPolicy(BaseModel):
    """Immutable policy for model-context assembly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reasoning_replay: ReasoningReplayMode = "active_run"
    compaction_trigger_ratio: float = Field(default=0.8, gt=0, le=1)
    compaction_retain_ratio: float = Field(default=0.16, gt=0, lt=1)
    summary_max_tokens: int = Field(default=8192, ge=64)
    compaction_reasoning_effort: ReasoningEffort = "low"
    max_tool_result_tokens: int = Field(default=2048, ge=64)
    max_total_tool_result_tokens: int = Field(default=16384, ge=64)
    token_safety_margin: float = Field(default=0.15, ge=0, le=1)

    @model_validator(mode="after")
    def validate_tool_budgets(self) -> "ContextPolicy":
        if self.compaction_retain_ratio >= self.compaction_trigger_ratio:
            raise ValueError(
                "compaction_retain_ratio must be smaller than "
                "compaction_trigger_ratio."
            )
        if self.max_tool_result_tokens > self.max_total_tool_result_tokens:
            raise ValueError(
                "max_tool_result_tokens cannot exceed max_total_tool_result_tokens."
            )
        return self


class ResultStoragePolicy(BaseModel):
    """Run-workspace storage policy for large capability results."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_inline_bytes: int = Field(default=256 * 1024, ge=1024)
    max_shell_output_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)


class ContextUsage(BaseModel):
    """Observable token estimate and reductions for one model request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_window_tokens: int = Field(ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    input_budget_tokens: int = Field(ge=1)
    compaction_trigger_tokens: int = Field(ge=1)
    compaction_retain_tokens: int = Field(ge=1)
    estimated_input_tokens: int = Field(ge=0)
    system_tokens: int = Field(default=0, ge=0)
    schema_tokens: int = Field(default=0, ge=0)
    summary_tokens: int = Field(default=0, ge=0)
    history_tokens: int = Field(default=0, ge=0)
    tool_result_tokens: int = Field(default=0, ge=0)
    included_items: int = Field(default=0, ge=0)
    compacted_items: int = Field(default=0, ge=0)
    truncated_tool_results: int = Field(default=0, ge=0)
    tool_result_metadata_tokens: int = Field(default=0, ge=0)
    tool_result_body_tokens: int = Field(default=0, ge=0)
    source_truncated_tool_results: int = Field(default=0, ge=0)
    unrecoverable_tool_results: int = Field(default=0, ge=0)
    estimator: Literal["heuristic", "custom", "vllm"] = "heuristic"
    server_max_model_len: int | None = Field(default=None, ge=1)
    model_context_window_tokens: int | None = Field(default=None, ge=1)
    configured_context_limit: int | None = Field(default=None, ge=1)
    reasoning_replay_mode: ReasoningReplayMode = "active_run"
    # These are context-projection statistics, not final HTTP serialization counts.
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


ReasoningOmissionReason: TypeAlias = Literal[
    "policy_none", "outside_active_run", "context_budget", "explicit_omit",
    "auto_unsupported", "request_override", "no_reasoning_available",
]


class RequestReasoning(BaseModel):
    """Text reasoning carried by a serialized request, not server-side usage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resolved_field: Literal["reasoning", "reasoning_content", "omit"]
    serialized_fields: tuple[Literal["reasoning", "reasoning_content", "omit"], ...]
    serialized_items: int = Field(ge=0)
    serialized_characters: int = Field(ge=0)
    omission_reasons: tuple[ReasoningOmissionReason, ...] = ()


class ModelCallMetadata(BaseModel):
    """Resolved protocol and reasoning controls for one provider call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal["chat_completions", "responses"]
    request_purpose: Literal["generation", "compaction"] = "generation"
    requested_reasoning_effort: str | None = None
    effective_reasoning_effort: str | None = None
    requested_max_output_tokens: int | None = Field(default=None, ge=1)
    effective_max_output_tokens: int | None = Field(default=None, ge=1)
    output_limit_field: Literal[
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
    ] | None = None
    ignored_parameters: tuple[str, ...] = ()
    fallback_reason: str | None = None
    # None means unobserved (including old persisted records), never zero sent.
    request_reasoning: RequestReasoning | None = None


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
    "ReasoningEffort",
    "ReasoningReplayMode",
    "RequestReasoning",
    "ResultStoragePolicy",
]
