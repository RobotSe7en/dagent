"""OpenAI-compatible Chat Completions and Responses provider."""

from __future__ import annotations

import asyncio
import json
import warnings
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import replace
from time import monotonic
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from httpx import TransportError

from dagent.config import ProviderConfig, ReasoningCapture
from dagent.providers.base import (
    ChatResponse,
    ChatStreamEvent,
    StructuredOutputFormat,
    ToolCall,
)
from dagent.providers.capabilities import (
    CapabilitySupport,
    ProtocolCapabilities,
    ProviderCapabilities,
)
from dagent.providers.model_io import (
    ModelCallMetadata,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelTokenCount,
    chat_messages_to_model_request,
    chat_response_from_model,
    model_request_to_chat,
    model_request_to_responses_input,
    normalize_model_response,
    responses_tools,
)
from dagent.providers.request_observation import observe_request_reasoning
from dagent.schemas.context import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    ModelCallAttempt,
    ModelTokenUsage,
    ReasoningEffort,
)


# Official API model limits, checked 2026-09-07. /models does not expose limits.
# https://api-docs.deepseek.com/quick_start/pricing/
# Exact window: https://api-docs.deepseek.com/quick_start/agent_integrations/codex/
_DEEPSEEK_CONTEXT_WINDOWS = {
    "deepseek-v4-flash": 1_048_576,
    "deepseek-v4-pro": 1_048_576,
    "deepseek-v4-flash-vision-exp": 1_048_576,
}


class ProviderCapabilityWarning(RuntimeWarning):
    """A requested optional provider feature could not be verified or used."""


class ProviderCapabilityError(RuntimeError):
    """The selected private endpoint cannot satisfy a model request."""


class ProviderTokenCountError(ProviderCapabilityError):
    """Exact token counting was unavailable or returned invalid data."""


class ProviderRequestError(RuntimeError):
    """A transport failure with public model-call attempt diagnostics."""

    def __init__(
        self, cause: Exception, *, metadata: ModelCallMetadata,
        response: ModelResponse | None = None,
    ) -> None:
        self.cause = cause
        self.metadata = metadata
        self.response = response
        self.status_code = getattr(cause, "status_code", None)
        super().__init__(f"{type(cause).__name__}: {cause}")


class ProviderResponseError(RuntimeError):
    """A model generation ended without a successful terminal state."""

    def __init__(
        self, status: str, details: Any = None, *,
        response: ModelResponse | None = None,
        reason: str | None = None,
    ) -> None:
        self.status = status
        self.details = details
        self.response = response
        self.reason = reason or status
        suffix = f": {details}" if details not in (None, "") else ""
        source = (
            "Chat Completions"
            if (isinstance(details, Mapping) and details.get("protocol") == "chat_completions")
            or (response is not None and response.metadata is not None
                and response.metadata.protocol == "chat_completions")
            else "Responses"
        )
        super().__init__(f"{source} generation ended with status '{status}'{suffix}")


class OpenAICompatibleProvider:
    """Provider for private OpenAI-compatible endpoints, with vLLM discovery.

    Construction is deliberately offline. Capability discovery happens on the
    first unified model call, token-count request, or explicit inspection.
    The legacy ``chat`` methods remain available and always use Chat
    Completions; new runtime code should use ``complete`` and ``stream``.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.config = config
        self.configured_context_window_tokens = config.context_window_tokens
        endpoint = urlsplit(config.base_url)
        self._is_deepseek_official = (
            endpoint.scheme == "https"
            and endpoint.hostname == "api.deepseek.com"
            and endpoint.port in (None, 443)
            and endpoint.path.rstrip("/") in ("", "/v1", "/beta")
        )
        self.model_context_window_tokens = (
            _DEEPSEEK_CONTEXT_WINDOWS.get(config.model)
            if self._is_deepseek_official
            else None
        )
        if (
            self.configured_context_window_tokens is not None
            and self.model_context_window_tokens is not None
            and self.configured_context_window_tokens > self.model_context_window_tokens
        ):
            raise ProviderCapabilityError(
                f"Configured context_window_tokens ({config.context_window_tokens}) "
                f"exceeds the official model context limit "
                f"({self.model_context_window_tokens}) for model '{config.model}'."
            )
        self.context_window_tokens = (
            self.configured_context_window_tokens
            or self.model_context_window_tokens
            or DEFAULT_CONTEXT_WINDOW_TOKENS
        )
        self.max_output_tokens = config.max_output_tokens
        self.client = client or AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
        )
        # Runtime retries are observable and never replay previously run tools.
        self.client = self.client.with_options(max_retries=0)
        self._capabilities: ProviderCapabilities | None = None
        self._capability_lock = asyncio.Lock()
        self._warning_keys: set[str] = set()
        self._tokenize_unavailable = False
        self.server_max_model_len: int | None = None

    @property
    def context_window_source(self) -> Literal["configured", "server", "model", "fallback"]:
        if self.configured_context_window_tokens is not None:
            return "configured"
        if self.server_max_model_len is not None:
            return "server"
        return "model" if self.model_context_window_tokens is not None else "fallback"

    async def _create_model_response(
        self, endpoint: Any, kwargs: dict[str, Any], metadata: ModelCallMetadata,
    ) -> tuple[Any, ModelCallMetadata]:
        started = monotonic()
        try:
            raw = await endpoint.with_raw_response.create(**kwargs)
        except (APIConnectionError, APIStatusError, OSError, TransportError) as exc:
            attempt = ModelCallAttempt(
                attempt=1, elapsed_seconds=monotonic() - started,
                exception_type=type(exc).__name__,
                http_status=getattr(exc, "status_code", None),
            )
            raise ProviderRequestError(
                exc, metadata=metadata.model_copy(update={"attempts": (attempt,)}),
            ) from exc
        attempt = ModelCallAttempt(
            attempt=1, elapsed_seconds=monotonic() - started,
            http_status=getattr(raw, "status_code", None),
        )
        return raw, metadata.model_copy(update={"attempts": (attempt,)})

    async def _read_model_stream(
        self, source: Any, partial: Callable[[], ModelResponse],
    ) -> AsyncIterator[Any]:
        try:
            async for item in source:
                yield item
        except (APIConnectionError, APIStatusError, OSError, TransportError) as exc:
            response = normalize_model_response(
                partial(), capture_tag_reasoning=self.config.reasoning_capture == "field_and_tags",
            )
            assert response.metadata is not None
            attempt = response.metadata.attempts[-1].model_copy(update={
                "exception_type": type(exc).__name__,
                "http_status": getattr(exc, "status_code", None) or response.metadata.attempts[-1].http_status,
            })
            metadata = response.metadata.model_copy(update={"attempts": (attempt,)})
            raise ProviderRequestError(
                exc, metadata=metadata, response=replace(response, metadata=metadata, status="failed"),
            ) from exc

    async def inspect_capabilities(self) -> ProviderCapabilities:
        """Probe and cache the endpoint's read-only OpenAPI/version metadata."""

        if self._capabilities is not None:
            return self._capabilities
        async with self._capability_lock:
            if self._capabilities is None:
                self._capabilities = await self._probe_capabilities()
        return self._capabilities

    async def complete(self, request: ModelRequest) -> ModelResponse:
        capabilities = await self.inspect_capabilities()
        protocol, reason = self._resolve_protocol(capabilities, request=request)
        if protocol == "responses":
            return await self._responses_complete(request, capabilities, reason)
        return await self._chat_complete(request, capabilities, reason)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        capabilities = await self.inspect_capabilities()
        protocol, reason = self._resolve_protocol(
            capabilities,
            request=request,
            stream=True,
        )
        if protocol == "responses":
            async for event in self._responses_stream(request, capabilities, reason):
                yield event
            return
        async for event in self._chat_stream(request, capabilities, reason):
            yield event

    async def context_reasoning_field(
        self,
        request: ModelRequest,
        stream: bool = False,
    ) -> Literal["reasoning", "reasoning_content", "omit"]:
        """Resolve the reasoning projection used by this exact request."""

        capabilities = await self.inspect_capabilities()
        protocol, _ = self._resolve_protocol(
            capabilities,
            request=request,
            stream=stream,
        )
        if protocol == "responses":
            return "reasoning"
        return self._resolved_chat_reasoning_field(capabilities)

    async def count_tokens(
        self,
        request: ModelRequest,
        reasoning_field: Literal["reasoning", "reasoning_content", "omit"] | None = None,
    ) -> ModelTokenCount | None:
        """Use vLLM's exact ``/tokenize`` endpoint when configured or available."""

        if self.config.token_counting == "heuristic":
            return None
        if self._is_deepseek_official:
            if self.config.token_counting == "vllm":
                raise ProviderTokenCountError(
                    "DeepSeek's official API does not provide vLLM /tokenize. "
                    "Use token_counting='auto' or 'heuristic'."
                )
            return None
        if self.config.token_counting == "auto" and self._tokenize_unavailable:
            return None
        capabilities = await self.inspect_capabilities()
        if capabilities.tokenize == "unsupported":
            message = "The server OpenAPI schema does not expose vLLM /tokenize."
            if self.config.token_counting == "vllm":
                raise ProviderTokenCountError(message)
            suffix = (
                " Using heuristic counting and the 131,072-token fallback context window."
                if self.configured_context_window_tokens is None
                else " Using heuristic counting and the configured context window without "
                "server-side validation."
            )
            self._warn_once("tokenize-unsupported", message + suffix)
            return None
        if reasoning_field is None:
            reasoning_field = await self.context_reasoning_field(request)
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": model_request_to_chat(
                request,
                reasoning_field=reasoning_field,
            ),
        }
        if request.tools:
            body["tools"] = list(request.tools)
        try:
            result = await self._post_root_json("/tokenize", body)
            if not isinstance(result, Mapping):
                raise ValueError("/tokenize must return an object.")
            count = result.get("count")
            if count is None:
                tokens = result.get("tokens")
                if not isinstance(tokens, list) or any(type(token) is not int or token < 0 for token in tokens):
                    raise ValueError("/tokenize must return an integer count or token ids.")
                count = len(tokens)
            if type(count) is not int or count < 0:
                raise ValueError("/tokenize count must be a nonnegative integer.")
            if count == 0 and (body["messages"] or body.get("tools")):
                raise ValueError("/tokenize returned zero for nonempty model input.")
            max_model_len_value = result.get("max_model_len")
            if max_model_len_value is not None and (type(max_model_len_value) is not int or max_model_len_value <= 0):
                raise ValueError("/tokenize max_model_len must be a positive integer.")
            max_model_len = max_model_len_value
            if max_model_len is not None:
                if (
                    self.configured_context_window_tokens is not None
                    and self.configured_context_window_tokens > max_model_len
                ):
                    raise ProviderCapabilityError(
                        "Configured context_window_tokens "
                        f"({self.configured_context_window_tokens}) exceeds vLLM "
                        f"max_model_len ({max_model_len}) for model "
                        f"'{self.config.model}'."
                    )
                self.server_max_model_len = max_model_len
                self.context_window_tokens = (
                    self.configured_context_window_tokens or max_model_len
                )
            elif self.configured_context_window_tokens is None and self.server_max_model_len is None:
                self.context_window_tokens = DEFAULT_CONTEXT_WINDOW_TOKENS
                self._warn_once(
                    "tokenize-missing-max-model-len",
                    "vLLM /tokenize did not return max_model_len; using the "
                    "131,072-token fallback context window.",
                )
            elif self.server_max_model_len is None:
                self._warn_once(
                    "tokenize-cannot-validate-context-window",
                    "vLLM /tokenize did not return max_model_len; using the "
                    "configured context window without server-side validation.",
                )
            return ModelTokenCount(
                count=count,
                max_model_len=max_model_len,
                estimator="vllm",
            )
        except ProviderCapabilityError:
            raise
        except (APIConnectionError, APIStatusError, OSError, TypeError, ValueError) as exc:
            message = f"vLLM /tokenize failed: {type(exc).__name__}: {exc}"
            if self.config.token_counting == "vllm":
                raise ProviderTokenCountError(message) from exc
            self._tokenize_unavailable = True
            suffix = (
                "; using heuristic counting and the last verified server context window."
                if self.context_window_source == "server" else
                "; using heuristic counting and the 131,072-token fallback window."
                if self.configured_context_window_tokens is None
                else "; using heuristic counting and the configured context window "
                "without server-side validation."
            )
            self._warn_once("tokenize-failed", message + suffix)
            return None

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        response_format: StructuredOutputFormat | None = None,
    ) -> ChatResponse:
        """Compatibility Chat Completions entrypoint without capability probing."""

        request = chat_messages_to_model_request(
            messages,
            tools=tools or (),
            response_format=response_format,
        )
        discovered = await self.inspect_capabilities()
        capabilities = discovered.model_copy(
            update={
                "resolved_protocol": "chat_completions",
                "resolution_reason": "chat() explicitly selects Chat Completions.",
            }
        )
        response = await self._chat_complete(
            request,
            capabilities,
            capabilities.resolution_reason,
        )
        return chat_response_from_model(response)

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        response_format: StructuredOutputFormat | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        """Compatibility streaming Chat Completions entrypoint."""

        request = chat_messages_to_model_request(
            messages,
            tools=tools or (),
            response_format=response_format,
        )
        discovered = await self.inspect_capabilities()
        capabilities = discovered.model_copy(
            update={
                "resolved_protocol": "chat_completions",
                "resolution_reason": "stream_chat() explicitly selects Chat Completions.",
            }
        )
        async for event in self._chat_stream(
            request,
            capabilities,
            capabilities.resolution_reason,
        ):
            yield ChatStreamEvent(
                type=event.type,
                content=event.content,
                channel=event.channel,
                response=(
                    chat_response_from_model(event.response)
                    if event.response is not None
                    else None
                ),
            )

    async def _chat_complete(
        self,
        request: ModelRequest,
        capabilities: ProviderCapabilities,
        resolution_reason: str,
    ) -> ModelResponse:
        kwargs, metadata = self._chat_kwargs(
            request,
            capabilities,
            resolution_reason=resolution_reason,
        )
        raw, metadata = await self._create_model_response(self.client.chat.completions, kwargs, metadata)
        metadata = self._observe_reasoning(raw.http_request.content, request, metadata, capabilities)
        response = raw.parse()
        choices = getattr(response, "choices", None) or []
        choice = choices[0] if choices else None
        message = getattr(choice, "message", None)
        metadata = metadata.model_copy(update={
            "finish_reason": getattr(choice, "finish_reason", None),
            "response_status": getattr(response, "status", None),
        })
        result = ModelResponse(
            content=str(getattr(message, "content", None) or ""),
            reasoning=_reasoning_content(message),
            refusal=str(getattr(message, "refusal", None) or ""),
            usage=_model_token_usage(getattr(response, "usage", None)),
            metadata=metadata,
        )
        result = normalize_model_response(
            result,
            capture_tag_reasoning=self.config.reasoning_capture == "field_and_tags",
        )
        raw_calls = getattr(message, "tool_calls", None) or []
        _require_chat_completion(result, raw_calls)
        return _with_tool_calls(result, raw_calls)

    async def _chat_stream(
        self,
        request: ModelRequest,
        capabilities: ProviderCapabilities,
        resolution_reason: str,
    ) -> AsyncIterator[ModelStreamEvent]:
        started = monotonic()
        kwargs, metadata = self._chat_kwargs(
            request,
            capabilities,
            resolution_reason=resolution_reason,
            stream=True,
        )
        raw, metadata = await self._create_model_response(self.client.chat.completions, kwargs, metadata)
        metadata = self._observe_reasoning(raw.http_request.content, request, metadata, capabilities)
        response_stream = raw.parse()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        refusal_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, str]] = {}
        token_usage: ModelTokenUsage | None = None
        finish_reason: str | None = None
        capture_tag_reasoning = self.config.reasoning_capture == "field_and_tags"
        thinking_parser = _ThinkingStreamParser(enabled=True)

        def partial() -> ModelResponse:
            attempt = metadata.attempts[-1].model_copy(update={"elapsed_seconds": monotonic() - started})
            pending_content = thinking_parser.buffer if not thinking_parser.in_reasoning else ""
            pending_reasoning = thinking_parser.buffer if thinking_parser.in_reasoning and capture_tag_reasoning else ""
            return ModelResponse(
                content="".join(content_parts) + pending_content,
                reasoning="".join(reasoning_parts) + pending_reasoning,
                refusal="".join(refusal_parts), usage=token_usage,
                metadata=metadata.model_copy(update={"finish_reason": finish_reason, "attempts": (attempt,)}),
            )

        async for chunk in self._read_model_stream(response_stream, partial):
            chunk_usage = _model_token_usage(getattr(chunk, "usage", None))
            if chunk_usage is not None:
                token_usage = chunk_usage
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            chunk_finish_reason = getattr(choice, "finish_reason", None)
            if chunk_finish_reason is not None:
                finish_reason = str(chunk_finish_reason)
            delta = choice.delta
            reasoning = _reasoning_content(delta)
            if reasoning:
                reasoning_parts.append(reasoning)
                yield ModelStreamEvent(
                    type="token",
                    channel="reasoning",
                    content=reasoning,
                )
            content = str(getattr(delta, "content", None) or "")
            if content:
                for channel, part in thinking_parser.feed(content):
                    if channel == "reasoning":
                        if capture_tag_reasoning:
                            reasoning_parts.append(part)
                    else:
                        content_parts.append(part)
                    if part and (channel != "reasoning" or capture_tag_reasoning):
                        yield ModelStreamEvent(
                            type="token",
                            channel=cast(Literal["reasoning", "content"], channel),
                            content=part,
                        )
            refusal = str(getattr(delta, "refusal", None) or "")
            if refusal:
                refusal_parts.append(refusal)
            _collect_streamed_tool_calls(
                tool_call_parts,
                getattr(delta, "tool_calls", None) or [],
            )

        for channel, part in thinking_parser.finish():
            if channel == "reasoning":
                if capture_tag_reasoning:
                    reasoning_parts.append(part)
            else:
                content_parts.append(part)
            if part and (channel != "reasoning" or capture_tag_reasoning):
                yield ModelStreamEvent(
                    type="token",
                    channel=cast(Literal["reasoning", "content"], channel),
                    content=part,
                )
        result = partial()
        raw_calls = [part for _, part in sorted(tool_call_parts.items())]
        _require_chat_completion(result, raw_calls)
        result = _with_tool_calls(result, raw_calls)
        yield ModelStreamEvent(type="done", response=result)

    async def _responses_complete(
        self,
        request: ModelRequest,
        capabilities: ProviderCapabilities,
        resolution_reason: str,
    ) -> ModelResponse:
        kwargs, metadata = self._responses_kwargs(
            request,
            capabilities,
            resolution_reason=resolution_reason,
        )
        raw, metadata = await self._create_model_response(self.client.responses, kwargs, metadata)
        metadata = self._observe_reasoning(raw.http_request.content, request, metadata, capabilities)
        response = raw.parse()
        result = _model_response_from_responses(
            response,
            metadata=metadata,
            capture_tag_reasoning=self.config.reasoning_capture == "field_and_tags",
        )
        return result

    async def _responses_stream(
        self,
        request: ModelRequest,
        capabilities: ProviderCapabilities,
        resolution_reason: str,
    ) -> AsyncIterator[ModelStreamEvent]:
        started = monotonic()
        kwargs, metadata = self._responses_kwargs(
            request,
            capabilities,
            resolution_reason=resolution_reason,
            stream=True,
        )
        raw, metadata = await self._create_model_response(self.client.responses, kwargs, metadata)
        metadata = self._observe_reasoning(raw.http_request.content, request, metadata, capabilities)
        response_stream = raw.parse()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        refusal_parts: list[str] = []
        tool_call_parts: dict[str, dict[str, str]] = {}
        completed_response: Any = None
        terminal_event: Any = None
        terminal_status: str | None = None

        def partial() -> ModelResponse:
            attempt = metadata.attempts[-1].model_copy(update={"elapsed_seconds": monotonic() - started})
            return ModelResponse(
                content="".join(content_parts), reasoning="".join(reasoning_parts),
                refusal="".join(refusal_parts), status=terminal_status or "unknown",
                metadata=metadata.model_copy(update={"attempts": (attempt,)}),
            )

        async for event in self._read_model_stream(response_stream, partial):
            event_type = str(getattr(event, "type", ""))
            delta = str(getattr(event, "delta", None) or "")
            if "reasoning" in event_type and event_type.endswith(".delta"):
                reasoning_parts.append(delta)
                if delta:
                    yield ModelStreamEvent(
                        type="token",
                        channel="reasoning",
                        content=delta,
                    )
            elif event_type == "response.output_text.delta":
                content_parts.append(delta)
                if delta:
                    yield ModelStreamEvent(
                        type="token",
                        channel="content",
                        content=delta,
                    )
            elif event_type == "response.refusal.delta":
                refusal_parts.append(delta)
            elif event_type in {
                "response.output_item.added",
                "response.output_item.done",
            }:
                item = getattr(event, "item", None)
                if item is not None and getattr(item, "type", None) == "function_call":
                    key = str(
                        getattr(item, "id", None)
                        or getattr(item, "call_id", None)
                        or getattr(event, "output_index", 0)
                    )
                    part = tool_call_parts.setdefault(
                        key,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    part["id"] = str(
                        getattr(item, "call_id", None)
                        or getattr(item, "id", None)
                        or part["id"]
                    )
                    part["name"] = str(getattr(item, "name", None) or part["name"])
                    arguments = str(getattr(item, "arguments", None) or "")
                    if arguments:
                        part["arguments"] = arguments
            elif event_type == "response.function_call_arguments.delta":
                key = str(
                    getattr(event, "item_id", None)
                    or getattr(event, "call_id", None)
                    or getattr(event, "output_index", 0)
                )
                part = tool_call_parts.setdefault(
                    key,
                    {
                        "id": str(getattr(event, "call_id", None) or ""),
                        "name": str(getattr(event, "name", None) or ""),
                        "arguments": "",
                    },
                )
                part["arguments"] += delta
            elif event_type in {
                "response.completed",
                "response.incomplete",
                "response.failed",
                "response.cancelled",
            }:
                terminal_event = event
                terminal_status = event_type.removeprefix("response.")
                completed_response = getattr(event, "response", None)
            elif event_type == "error":
                terminal_event = event
                terminal_status = "failed"

        if completed_response is not None:
            try:
                result = _model_response_from_responses(
                    completed_response,
                    metadata=partial().metadata,
                    capture_tag_reasoning=self.config.reasoning_capture == "field_and_tags",
                )
            except ProviderResponseError as exc:
                if exc.response is not None:
                    received = normalize_model_response(
                        partial(), capture_tag_reasoning=self.config.reasoning_capture == "field_and_tags",
                    )
                    exc.response = replace(
                        exc.response,
                        content=exc.response.content or received.content,
                        reasoning=exc.response.reasoning or received.reasoning,
                        refusal=exc.response.refusal or received.refusal,
                    )
                raise
        else:
            result = normalize_model_response(
                partial(), capture_tag_reasoning=self.config.reasoning_capture == "field_and_tags",
            )
        if terminal_status != "completed":
            raise ProviderResponseError(
                terminal_status or "incomplete",
                _response_details(completed_response or terminal_event),
                response=replace(result, status=terminal_status or "unknown"),
                reason="missing_terminal_response" if terminal_status is None else terminal_status,
            )
        if completed_response is None:
            result = _with_tool_calls(result, list(tool_call_parts.values()))
        yield ModelStreamEvent(type="done", response=result)

    def _observe_reasoning(
        self, body: bytes, request: ModelRequest, metadata: ModelCallMetadata,
        capabilities: ProviderCapabilities,
    ) -> ModelCallMetadata:
        return observe_request_reasoning(
            body, request, metadata,
            resolved_field=("reasoning" if metadata.protocol == "responses"
                            else self._resolved_chat_reasoning_field(capabilities)),
            explicit_omit=self.config.chat_reasoning_field == "omit",
        )

    def _chat_kwargs(
        self,
        request: ModelRequest,
        capabilities: ProviderCapabilities,
        *,
        resolution_reason: str,
        stream: bool = False,
    ) -> tuple[dict[str, Any], ModelCallMetadata]:
        reasoning_field = self._resolved_chat_reasoning_field(capabilities)
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": model_request_to_chat(
                request,
                reasoning_field=reasoning_field,
            ),
        }
        if request.tools:
            kwargs["tools"] = list(request.tools)
        if stream:
            kwargs["stream"] = True
            if self.config.stream_include_usage:
                kwargs["stream_options"] = {"include_usage": True}
        extra_body: dict[str, Any] = {}
        reasoning_effort = self._effective_reasoning_effort(request)
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        if reasoning_effort is not None:
            extra_body["include_reasoning"] = True
        max_output_tokens = self._effective_max_output_tokens(request)
        requested_max_output_tokens = self._requested_max_output_tokens(request)
        output_limit_field = None
        if max_output_tokens is not None:
            output_limit_field = (
                capabilities.chat_completions.output_limit_field or "max_tokens"
            )
            kwargs[output_limit_field] = max_output_tokens
        extra_body = _merge_dicts(extra_body, self.config.extra_body)
        extra_body.pop("store", None)
        extra_body.pop("previous_response_id", None)
        if extra_body:
            kwargs["extra_body"] = extra_body
        kwargs.update(self.config.extra_request_args)
        if request.response_format is not None:
            kwargs["response_format"] = _chat_response_format(request.response_format)
        metadata = ModelCallMetadata(
            protocol="chat_completions",
            request_purpose=request.purpose,
            requested_reasoning_effort=reasoning_effort,
            effective_reasoning_effort=(
                str(kwargs.get("reasoning_effort"))
                if kwargs.get("reasoning_effort") is not None
                else None
            ),
            requested_max_output_tokens=requested_max_output_tokens,
            effective_max_output_tokens=max_output_tokens,
            output_limit_field=output_limit_field,
            fallback_reason=resolution_reason,
        )
        return kwargs, metadata

    def _responses_kwargs(
        self,
        request: ModelRequest,
        capabilities: ProviderCapabilities,
        *,
        resolution_reason: str,
        stream: bool = False,
    ) -> tuple[dict[str, Any], ModelCallMetadata]:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "input": model_request_to_responses_input(request),
            "store": False,
        }
        if request.instructions:
            kwargs["instructions"] = request.instructions
        if request.tools:
            kwargs["tools"] = responses_tools(request.tools)
        if stream:
            kwargs["stream"] = True
        extra_body: dict[str, Any] = {}
        reasoning_effort = self._effective_reasoning_effort(request)
        if reasoning_effort is not None:
            kwargs["reasoning"] = {"effort": reasoning_effort}
        if reasoning_effort is not None and capabilities.server_kind == "vllm":
            extra_body["include_reasoning"] = True
        max_output_tokens = self._effective_max_output_tokens(request)
        requested_max_output_tokens = self._requested_max_output_tokens(request)
        if max_output_tokens is not None:
            kwargs["max_output_tokens"] = max_output_tokens
        extra_body = _merge_dicts(extra_body, self.config.extra_body)
        extra_body.pop("store", None)
        extra_body.pop("previous_response_id", None)
        if extra_body:
            kwargs["extra_body"] = extra_body
        kwargs.update(self.config.extra_request_args)
        # Stateless Responses is an SDK invariant. Callers cannot accidentally
        # introduce server-side state or an incompatible previous-response chain.
        kwargs["store"] = False
        kwargs.pop("previous_response_id", None)
        include = kwargs.get("include")
        if isinstance(include, Sequence) and not isinstance(include, (str, bytes)):
            filtered_include = [
                value
                for value in include
                if str(value) != "reasoning.encrypted_content"
            ]
            if filtered_include:
                kwargs["include"] = filtered_include
            else:
                kwargs.pop("include", None)
        elif include is not None and str(include) == "reasoning.encrypted_content":
            kwargs.pop("include", None)
        if request.response_format is not None:
            kwargs["text"] = {
                "format": _responses_text_format(request.response_format)
            }
        metadata = ModelCallMetadata(
            protocol="responses",
            request_purpose=request.purpose,
            requested_reasoning_effort=reasoning_effort,
            effective_reasoning_effort=reasoning_effort,
            requested_max_output_tokens=requested_max_output_tokens,
            effective_max_output_tokens=max_output_tokens,
            output_limit_field=(
                "max_output_tokens" if max_output_tokens is not None else None
            ),
            fallback_reason=resolution_reason,
        )
        return kwargs, metadata

    def _effective_reasoning_effort(self, request: ModelRequest) -> str | None:
        if request.reasoning_effort is not None:
            return request.reasoning_effort
        return self.config.reasoning_effort

    def _effective_max_output_tokens(self, request: ModelRequest) -> int | None:
        configured = self.config.max_output_tokens
        requested = request.max_output_tokens
        if configured is not None and requested is not None:
            return min(configured, requested)
        if requested is not None:
            return requested
        if request.inherit_provider_max_output_tokens:
            return configured
        return None

    def _requested_max_output_tokens(self, request: ModelRequest) -> int | None:
        if (
            request.max_output_tokens is not None
            or not request.inherit_provider_max_output_tokens
        ):
            return request.max_output_tokens
        return self.config.max_output_tokens

    def _resolved_chat_reasoning_field(
        self,
        capabilities: ProviderCapabilities,
    ) -> Literal["reasoning", "reasoning_content", "omit"]:
        configured = self.config.chat_reasoning_field
        if configured != "auto":
            return cast(Literal["reasoning", "reasoning_content", "omit"], configured)
        # The official V4 Chat tool protocol replays assistant reasoning_content.
        # An open-weight model name on a proxy does not establish its wire format.
        if self._is_deepseek_official and self.config.model in _DEEPSEEK_CONTEXT_WINDOWS:
            return "reasoning_content"
        if capabilities.server_kind == "vllm":
            return "reasoning"
        return "omit"

    def _resolve_protocol(
        self,
        capabilities: ProviderCapabilities,
        *,
        request: ModelRequest | None = None,
        stream: bool = False,
    ) -> tuple[Literal["chat_completions", "responses"], str]:
        configured = self.config.protocol
        if configured != "auto":
            protocol = cast(Literal["chat_completions", "responses"], configured)
            selected = (
                capabilities.chat_completions
                if protocol == "chat_completions"
                else capabilities.responses
            )
            if selected.endpoint == "unsupported":
                raise ProviderCapabilityError(
                    f"Configured protocol={protocol} is not exposed by the server."
                )
            required = self._required_capabilities(
                request,
                stream=stream,
                protocol=protocol,
                capabilities=capabilities,
            )
            unsupported = tuple(
                name
                for name in required
                if getattr(selected, name) == "unsupported"
            )
            if unsupported:
                raise ProviderCapabilityError(
                    f"Configured protocol={protocol} does not support required request "
                    "capabilities: " + ", ".join(unsupported) + "."
                )
            return protocol, (
                f"protocol={configured} was explicitly configured; cross-protocol "
                "fallback is disabled."
            )

        chat = capabilities.chat_completions.endpoint
        responses = capabilities.responses.endpoint
        responses_required = self._required_capabilities(
            request,
            stream=stream,
            protocol="responses",
            capabilities=capabilities,
        )
        chat_required = self._required_capabilities(
            request,
            stream=stream,
            protocol="chat_completions",
            capabilities=capabilities,
        )
        if responses == "supported":
            missing_responses = _missing_capabilities(
                capabilities.responses,
                responses_required,
            )
            if not missing_responses:
                return "responses", "Responses supports the current request and is preferred."
            if chat == "supported" and not _missing_capabilities(
                capabilities.chat_completions,
                chat_required,
            ):
                return (
                    "chat_completions",
                    "Chat Completions supports request capabilities missing from "
                    f"Responses: {', '.join(missing_responses)}.",
                )
            raise ProviderCapabilityError(
                "Neither discovered protocol supports all required request capabilities: "
                + ", ".join(missing_responses)
                + "."
            )
        if chat == "supported":
            missing_chat = _missing_capabilities(
                capabilities.chat_completions,
                chat_required,
            )
            if missing_chat:
                raise ProviderCapabilityError(
                    "Chat Completions does not support required request capabilities: "
                    + ", ".join(missing_chat)
                    + "."
                )
            return "chat_completions", "Only Chat Completions was discovered."
        self._warn_once(
            "protocol-probe-fallback",
            "Provider capability probing was inconclusive; using Chat Completions.",
        )
        return (
            "chat_completions",
            "Capability probing was inconclusive; auto mode conservatively selected Chat Completions.",
        )

    def _required_capabilities(
        self,
        request: ModelRequest | None,
        *,
        stream: bool,
        protocol: Literal["chat_completions", "responses"],
        capabilities: ProviderCapabilities,
    ) -> tuple[str, ...]:
        required: list[str] = []
        if request is not None and request.tools:
            required.append("tools")
        if stream:
            required.append("streaming")
        if request is not None and request.response_format is not None:
            required.append("structured_output")
        reasoning_effort = (
            self._effective_reasoning_effort(request)
            if request is not None
            else self.config.reasoning_effort
        )
        replayed_reasoning = bool(
            request is not None
            and any(getattr(item, "reasoning", "") for item in request.items)
        )
        sends_replayed_reasoning = replayed_reasoning and (
            protocol == "responses"
            or self._resolved_chat_reasoning_field(capabilities) != "omit"
        )
        if (
            reasoning_effort is not None
            or sends_replayed_reasoning
        ):
            required.append("reasoning")
        if reasoning_effort is not None:
            required.append("reasoning_effort")
        if request is not None and self._effective_max_output_tokens(request) is not None:
            required.append("output_limit")
        return tuple(required)

    async def _probe_capabilities(self) -> ProviderCapabilities:
        openapi: Mapping[str, Any] | None = None
        version: str | None = None
        try:
            raw_openapi = await self._get_root_json("/openapi.json")
            if isinstance(raw_openapi, Mapping):
                openapi = raw_openapi
        except (APIConnectionError, APIStatusError, OSError, TypeError, ValueError) as exc:
            self._warn_once(
                "openapi-probe",
                f"Provider OpenAPI discovery failed ({type(exc).__name__}: {exc}).",
            )
        try:
            raw_version = await self._get_root_json("/version")
            if isinstance(raw_version, Mapping) and raw_version.get("version"):
                version = str(raw_version["version"])
        except (APIConnectionError, APIStatusError, OSError, TypeError, ValueError):
            pass

        if openapi is None:
            configured = self.config.protocol
            resolved = "chat_completions" if configured == "auto" else configured
            return _unknown_capabilities(
                resolved_protocol=cast(
                    Literal["chat_completions", "responses"], resolved
                ),
                reason="OpenAPI capability discovery was unavailable.",
                server_kind="vllm" if version is not None else "unknown",
                server_version=version,
            )

        paths = openapi.get("paths")
        paths = paths if isinstance(paths, Mapping) else {}
        title = str(
            cast(Mapping[str, Any], openapi.get("info") or {}).get("title") or ""
        )
        server_kind: Literal["vllm", "unknown"] = (
            "vllm" if version is not None or "vllm" in title.lower() else "unknown"
        )
        chat = _protocol_capabilities(openapi, "/v1/chat/completions")
        responses = _protocol_capabilities(openapi, "/v1/responses")
        tokenize: CapabilitySupport = (
            "supported" if "/tokenize" in paths else "unsupported"
        )
        provisional = ProviderCapabilities(
            server_kind=server_kind,
            server_version=version,
            chat_completions=chat,
            responses=responses,
            tokenize=tokenize,
            resolved_protocol="chat_completions",
            resolution_reason="Capability discovery completed.",
        )
        protocol, reason = self._resolve_protocol(provisional)
        return provisional.model_copy(
            update={"resolved_protocol": protocol, "resolution_reason": reason}
        )

    async def _get_root_json(self, path: str) -> Mapping[str, Any]:
        client = self._root_client()
        value = await client.get(path, cast_to=object)
        if not isinstance(value, Mapping):
            raise TypeError(f"{path} did not return a JSON object.")
        return value

    async def _post_root_json(
        self,
        path: str,
        body: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        client = self._root_client()
        value = await client.post(path, cast_to=object, body=dict(body))
        if not isinstance(value, Mapping):
            raise TypeError(f"{path} did not return a JSON object.")
        return value

    def _root_client(self) -> Any:
        with_options = getattr(self.client, "with_options", None)
        if not callable(with_options):
            raise TypeError("The configured OpenAI client does not support raw requests.")
        base_url = self.config.base_url.rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        return with_options(base_url=base_url + "/")

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warning_keys:
            return
        self._warning_keys.add(key)
        warnings.warn(message, ProviderCapabilityWarning, stacklevel=3)


class Provider(OpenAICompatibleProvider):
    """Public OpenAI-compatible provider SDK entrypoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float = 60,
        protocol: Literal["auto", "chat_completions", "responses"] = "auto",
        token_counting: Literal["auto", "vllm", "heuristic"] = "auto",
        chat_reasoning_field: Literal[
            "auto", "reasoning", "reasoning_content", "omit"
        ] = "auto",
        reasoning_effort: ReasoningEffort | None = None,
        reasoning_capture: ReasoningCapture = "field_and_tags",
        stream_include_usage: bool = False,
        context_window_tokens: int | None = None,
        max_output_tokens: int | None = None,
        extra_request_args: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        super().__init__(
            ProviderConfig(
                base_url=base_url,
                model=model,
                api_key=api_key,
                api_key_env=api_key_env,
                timeout_seconds=timeout_seconds,
                protocol=protocol,
                token_counting=token_counting,
                chat_reasoning_field=chat_reasoning_field,
                reasoning_effort=reasoning_effort,
                reasoning_capture=reasoning_capture,
                stream_include_usage=stream_include_usage,
                context_window_tokens=context_window_tokens,
                max_output_tokens=max_output_tokens,
                extra_request_args=extra_request_args or {},
                extra_body=extra_body or {},
            ),
            client=client,
        )


def _unknown_capabilities(
    *,
    resolved_protocol: Literal["chat_completions", "responses"],
    reason: str,
    server_kind: Literal["vllm", "unknown"] = "unknown",
    server_version: str | None = None,
) -> ProviderCapabilities:
    return ProviderCapabilities(
        server_kind=server_kind,
        server_version=server_version,
        resolved_protocol=resolved_protocol,
        resolution_reason=reason,
    )


def _protocol_capabilities(
    openapi: Mapping[str, Any],
    path: str,
) -> ProtocolCapabilities:
    paths = openapi.get("paths")
    paths = paths if isinstance(paths, Mapping) else {}
    operation = paths.get(path)
    if not isinstance(operation, Mapping):
        return ProtocolCapabilities(
            endpoint="unsupported",
            reasoning="unsupported",
            reasoning_effort="unsupported",
            output_limit="unsupported",
            tools="unsupported",
            streaming="unsupported",
            structured_output="unsupported",
        )
    names = _request_property_names(openapi, operation)
    output_limit_field: Literal[
        "max_tokens", "max_completion_tokens", "max_output_tokens"
    ] | None = None
    if path.endswith("/chat/completions"):
        if "max_completion_tokens" in names:
            output_limit_field = "max_completion_tokens"
        elif "max_tokens" in names:
            output_limit_field = "max_tokens"
    elif "max_output_tokens" in names:
        output_limit_field = "max_output_tokens"
    return ProtocolCapabilities(
        endpoint="supported",
        reasoning=_field_support(names, {"reasoning", "include_reasoning"}),
        reasoning_effort=_field_support(names, {"reasoning_effort", "reasoning"}),
        output_limit=("supported" if output_limit_field is not None else "unsupported"),
        output_limit_field=output_limit_field,
        tools=_field_support(names, {"tools"}),
        streaming=_field_support(names, {"stream"}),
        structured_output=_field_support(names, {"response_format", "text"}),
    )


def _field_support(names: set[str], candidates: set[str]) -> CapabilitySupport:
    return "supported" if names.intersection(candidates) else "unsupported"


def _missing_capabilities(
    capabilities: ProtocolCapabilities,
    required: Sequence[str],
) -> tuple[str, ...]:
    return tuple(
        name
        for name in required
        if getattr(capabilities, name) != "supported"
    )


def _request_property_names(
    openapi: Mapping[str, Any],
    path_item: Mapping[str, Any],
) -> set[str]:
    operation = path_item.get("post")
    if not isinstance(operation, Mapping):
        return set()
    request_body = operation.get("requestBody")
    if not isinstance(request_body, Mapping):
        return set()
    content = request_body.get("content")
    if not isinstance(content, Mapping):
        return set()
    media = content.get("application/json")
    if not isinstance(media, Mapping):
        return set()
    schema = media.get("schema")
    return _schema_property_names(openapi, schema, seen=set())


def _schema_property_names(
    openapi: Mapping[str, Any],
    schema: Any,
    *,
    seen: set[str],
) -> set[str]:
    if not isinstance(schema, Mapping):
        return set()
    reference = schema.get("$ref")
    if isinstance(reference, str):
        if reference in seen:
            return set()
        seen.add(reference)
        target: Any = openapi
        for segment in reference.removeprefix("#/").split("/"):
            if not isinstance(target, Mapping):
                return set()
            target = target.get(segment.replace("~1", "/").replace("~0", "~"))
        return _schema_property_names(openapi, target, seen=seen)
    names: set[str] = set()
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        names.update(str(name) for name in properties)
    for compound in ("allOf", "anyOf", "oneOf"):
        children = schema.get(compound)
        if isinstance(children, Sequence) and not isinstance(children, (str, bytes)):
            for child in children:
                names.update(_schema_property_names(openapi, child, seen=seen))
    return names


def _chat_response_format(value: StructuredOutputFormat) -> dict[str, Any]:
    json_schema: dict[str, Any] = {
        "name": value.name,
        "schema": value.schema,
        "strict": value.strict,
    }
    if value.description:
        json_schema["description"] = value.description
    return {"type": "json_schema", "json_schema": json_schema}


def _responses_text_format(value: StructuredOutputFormat) -> dict[str, Any]:
    output: dict[str, Any] = {
        "type": "json_schema",
        "name": value.name,
        "schema": value.schema,
        "strict": value.strict,
    }
    if value.description:
        output["description"] = value.description
    return output


def _model_response_from_responses(
    response: Any,
    *,
    metadata: ModelCallMetadata,
    capture_tag_reasoning: bool,
) -> ModelResponse:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    refusal_parts: list[str] = []
    raw_tool_calls: list[dict[str, str]] = []
    for item in getattr(response, "output", None) or []:
        item_type = str(getattr(item, "type", ""))
        if item_type == "reasoning":
            for part in (
                list(getattr(item, "content", None) or [])
                + list(getattr(item, "summary", None) or [])
            ):
                text = getattr(part, "text", None)
                if text:
                    reasoning_parts.append(str(text))
        elif item_type == "message":
            for part in getattr(item, "content", None) or []:
                part_type = str(getattr(part, "type", ""))
                if part_type == "output_text":
                    content_parts.append(str(getattr(part, "text", None) or ""))
                elif part_type == "refusal":
                    refusal_parts.append(str(getattr(part, "refusal", None) or ""))
        elif item_type == "function_call":
            raw_tool_calls.append({
                "id": str(getattr(item, "call_id", None) or getattr(item, "id", "")),
                "name": str(getattr(item, "name", "")),
                "arguments": getattr(item, "arguments", None) or "{}",
            })
    status = getattr(response, "status", None)
    result = ModelResponse(
        content="".join(content_parts),
        reasoning="".join(reasoning_parts),
        refusal="".join(refusal_parts),
        usage=_model_token_usage(getattr(response, "usage", None)),
        status=str(status or "unknown"),
        metadata=metadata.model_copy(update={"response_status": status}),
    )
    result = normalize_model_response(
        result,
        capture_tag_reasoning=capture_tag_reasoning,
    )
    _require_completed_response(result, response, raw_tool_calls)
    return _with_tool_calls(result, raw_tool_calls)


def _require_completed_response(
    response: ModelResponse, raw_response: Any, raw_calls: Sequence[Any] = (),
) -> None:
    if response.status == "completed":
        return
    details = _response_details(raw_response)
    reason = details.get("reason") if isinstance(details, Mapping) else None
    error_details = dict(details) if isinstance(details, Mapping) else {}
    if details is not None and not isinstance(details, Mapping):
        error_details["server_details"] = details
    error_details.update(protocol="responses", tool_calls=_raw_tool_call_parts(raw_calls))
    raise ProviderResponseError(
        "incomplete" if response.status == "unknown" else response.status,
        error_details,
        response=response,
        reason="missing_terminal_response" if response.status == "unknown" else reason or response.status,
    )


def _require_chat_completion(response: ModelResponse, raw_calls: Sequence[Any]) -> None:
    finish = response.metadata.finish_reason if response.metadata else None
    if response.metadata is not None and response.metadata.response_status in {"failed", "incomplete", "cancelled"}:
        status = response.metadata.response_status
        raise ProviderResponseError(status, response=response, reason=status)
    if finish in {"stop", "tool_calls"} or (finish == "content_filter" and response.refusal.strip()):
        return
    reason = (
        "max_output_tokens" if finish == "length" else
        "missing_finish_reason" if finish is None else
        "content_filter" if finish == "content_filter" else "invalid_finish_reason"
    )
    status = "incomplete" if finish in (None, "length") else "failed"
    raise ProviderResponseError(
        status, {
            "protocol": "chat_completions", "finish_reason": finish, "reason": reason,
            "tool_calls": _raw_tool_call_parts(raw_calls),
        },
        response=replace(response, status=status), reason=reason,
    )


def _raw_tool_call_parts(raw_calls: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        call if isinstance(call, dict) else {
            "id": getattr(call, "id", None) or "",
            "name": getattr(getattr(call, "function", None), "name", None) or "",
            "arguments": getattr(getattr(call, "function", None), "arguments", None) or "{}",
        }
        for call in raw_calls
    ]


def _with_tool_calls(response: ModelResponse, raw_calls: Sequence[Any]) -> ModelResponse:
    parts = _raw_tool_call_parts(raw_calls)
    try:
        calls = tuple(_convert_streamed_tool_call(part) for part in parts)
        ids = [call.id for call in calls]
        if any(not call.id.strip() or not call.name.strip() for call in calls) or len(ids) != len(set(ids)):
            raise ValueError("Model tool calls require nonempty names and unique ids.")
    except (TypeError, ValueError) as exc:
        raise ProviderResponseError(
            "failed", {
                "protocol": response.metadata.protocol if response.metadata else None,
                "reason": "invalid_tool_call", "message": str(exc), "tool_calls": parts,
            },
            response=replace(response, status="failed"), reason="invalid_tool_call",
        ) from exc
    return replace(response, tool_calls=calls)


def _response_details(value: Any) -> Any:
    if value is None:
        return None
    if getattr(value, "type", None) == "error":
        model_dump = getattr(value, "model_dump", None)
        return model_dump(mode="json") if callable(model_dump) else vars(value)
    for name in ("error", "incomplete_details"):
        details = getattr(value, name, None)
        if details is None:
            continue
        model_dump = getattr(details, "model_dump", None)
        if callable(model_dump):
            return model_dump(mode="json")
        return vars(details) if hasattr(details, "__dict__") else details
    return None


def _parse_tool_arguments(raw_arguments: str | None) -> dict[str, Any]:
    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("Model tool-call arguments are not valid JSON.") from exc
    if not isinstance(arguments, dict):
        raise ValueError("Model tool-call arguments must decode to a JSON object.")
    return arguments


def _convert_streamed_tool_call(tool_call: dict[str, str]) -> ToolCall:
    if not isinstance(tool_call["id"], str) or not isinstance(tool_call["name"], str):
        raise ValueError("Model tool-call ids and names must be strings.")
    return ToolCall(
        id=tool_call["id"],
        name=tool_call["name"],
        arguments=_parse_tool_arguments(tool_call["arguments"]),
    )


def _collect_streamed_tool_calls(
    parts: dict[int, dict[str, str]],
    tool_calls: Sequence[Any],
) -> None:
    for tool_call in tool_calls:
        index = int(getattr(tool_call, "index", 0) or 0)
        part = parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if getattr(tool_call, "id", None):
            part["id"] += str(tool_call.id)
        function = getattr(tool_call, "function", None)
        if function is not None:
            if getattr(function, "name", None):
                part["name"] += str(function.name)
            if getattr(function, "arguments", None):
                part["arguments"] += str(function.arguments)


def _reasoning_content(item: Any) -> str:
    reasoning = (
        getattr(item, "reasoning", None)
        or getattr(item, "reasoning_content", None)
        or ""
    )
    return str(reasoning)


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _model_token_usage(value: Any) -> ModelTokenUsage | None:
    if value is None:
        return None
    input_tokens = getattr(value, "prompt_tokens", None)
    if input_tokens is None:
        input_tokens = getattr(value, "input_tokens", None)
    output_tokens = getattr(value, "completion_tokens", None)
    if output_tokens is None:
        output_tokens = getattr(value, "output_tokens", None)
    details = (
        getattr(value, "completion_tokens_details", None)
        or getattr(value, "output_tokens_details", None)
    )
    reasoning_tokens = getattr(details, "reasoning_tokens", None)
    total_tokens = getattr(value, "total_tokens", None)
    return ModelTokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
    )


class _ThinkingStreamParser:
    """Split think tags correctly even when delimiters span stream chunks."""

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.in_reasoning = False
        self.buffer = ""
        self.strip_content_whitespace = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        if not self.enabled:
            return [("content", chunk)]
        self.buffer += chunk
        output: list[tuple[str, str]] = []
        while self.buffer:
            delimiter = self._CLOSE if self.in_reasoning else self._OPEN
            index = self.buffer.lower().find(delimiter)
            channel = "reasoning" if self.in_reasoning else "content"
            if index >= 0:
                if index:
                    self._append(output, channel, self.buffer[:index])
                self.buffer = self.buffer[index + len(delimiter):]
                self.in_reasoning = not self.in_reasoning
                if not self.in_reasoning:
                    self.strip_content_whitespace = True
                continue
            keep = min(len(self.buffer), len(delimiter) - 1)
            emit_length = len(self.buffer) - keep
            if emit_length:
                self._append(output, channel, self.buffer[:emit_length])
                self.buffer = self.buffer[emit_length:]
            break
        return output

    def finish(self) -> list[tuple[str, str]]:
        if not self.buffer:
            return []
        channel = "reasoning" if self.in_reasoning else "content"
        value = self.buffer
        self.buffer = ""
        output: list[tuple[str, str]] = []
        self._append(output, channel, value)
        return output

    def _append(
        self,
        output: list[tuple[str, str]],
        channel: str,
        value: str,
    ) -> None:
        if channel == "content" and self.strip_content_whitespace:
            value = value.lstrip()
            if value:
                self.strip_content_whitespace = False
        if value:
            output.append((channel, value))


__all__ = [
    "OpenAICompatibleProvider",
    "Provider",
    "ProviderCapabilityWarning",
    "ProviderCapabilityError",
    "ProviderResponseError",
    "ProviderRequestError",
    "ProviderTokenCountError",
]
