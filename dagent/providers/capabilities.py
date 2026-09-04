"""Read-only capability contracts for OpenAI-compatible providers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


CapabilitySupport = Literal["supported", "unsupported", "unknown"]


class ProtocolCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: CapabilitySupport = "unknown"
    reasoning: CapabilitySupport = "unknown"
    reasoning_effort: CapabilitySupport = "unknown"
    output_limit: CapabilitySupport = "unknown"
    output_limit_field: Literal[
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
    ] | None = None
    tools: CapabilitySupport = "unknown"
    streaming: CapabilitySupport = "unknown"
    structured_output: CapabilitySupport = "unknown"


class ProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    server_kind: Literal["vllm", "unknown"] = "unknown"
    server_version: str | None = None
    chat_completions: ProtocolCapabilities = Field(
        default_factory=ProtocolCapabilities
    )
    responses: ProtocolCapabilities = Field(default_factory=ProtocolCapabilities)
    tokenize: CapabilitySupport = "unknown"
    resolved_protocol: Literal["chat_completions", "responses"]
    resolution_reason: str


__all__ = [
    "CapabilitySupport",
    "ProtocolCapabilities",
    "ProviderCapabilities",
]
