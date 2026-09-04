from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI

from dagent.config import ProviderConfig
from dagent.providers import (
    OpenAICompatibleProvider,
    ProviderCapabilityError,
    ProviderCapabilityWarning,
    ProviderResponseError,
    StructuredOutputFormat,
    ToolCall,
)
from dagent.providers.model_io import (
    ModelAssistantTurn,
    ModelRequest,
    ModelToolResultInput,
    ModelUserInput,
)


def _openapi(*, responses_output: bool = True, tokenize: bool = True) -> dict:
    response_properties = {
        "input": {},
        "tools": {},
        "stream": {},
        "reasoning": {},
        "text": {},
    }
    if responses_output:
        response_properties["max_output_tokens"] = {}
    paths = {
        "/v1/chat/completions": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ChatRequest"}
                        }
                    }
                }
            }
        },
        "/v1/responses": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"properties": response_properties}
                        }
                    }
                }
            }
        },
    }
    if tokenize:
        paths["/tokenize"] = {"post": {}}
    return {
        "info": {"title": "vLLM OpenAI-Compatible API"},
        "paths": paths,
        "components": {
            "schemas": {
                "ChatRequest": {
                    "properties": {
                        "messages": {},
                        "tools": {},
                        "stream": {},
                        "reasoning_effort": {},
                        "max_completion_tokens": {},
                        "include_reasoning": {},
                        "response_format": {},
                    }
                }
            }
        },
    }


class _Completions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return _AsyncStream(
                [
                    SimpleNamespace(
                        usage=None,
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content="chat stream",
                                    reasoning=None,
                                    refusal=None,
                                    tool_calls=[],
                                )
                            )
                        ],
                    )
                ]
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="chat answer",
                        reasoning="chat reasoning",
                        tool_calls=[],
                    )
                )
            ],
            usage=None,
        )


class _Responses:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if kwargs.get("stream"):
            return _AsyncStream(
                [
                    SimpleNamespace(
                        type="response.reasoning_text.delta",
                        delta="stream thought",
                    ),
                    SimpleNamespace(
                        type="response.output_text.delta",
                        delta="stream answer",
                    ),
                    SimpleNamespace(
                        type="response.output_item.added",
                        output_index=1,
                        item=SimpleNamespace(
                            type="function_call",
                            id="fc_stream",
                            call_id="call_stream",
                            name="lookup",
                            arguments="",
                        ),
                    ),
                    SimpleNamespace(
                        type="response.function_call_arguments.delta",
                        item_id="fc_stream",
                        delta='{"query":"stream"}',
                    ),
                ]
            )
        return SimpleNamespace(
            status="completed",
            output=[
                SimpleNamespace(
                    type="reasoning",
                    content=[SimpleNamespace(text="response reasoning")],
                    summary=[],
                ),
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="response answer")],
                ),
                SimpleNamespace(
                    type="function_call",
                    call_id="call_next",
                    id="fc_next",
                    name="lookup",
                    arguments='{"query":"next"}',
                ),
            ],
            usage=None,
        )


class _AsyncStream:
    def __init__(self, values: list) -> None:
        self.values = values

    def __aiter__(self):
        self.iterator = iter(self.values)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class DualProtocolClient:
    def __init__(
        self,
        *,
        openapi: dict | None = None,
        responses_error: Exception | None = None,
    ) -> None:
        self.openapi = openapi or _openapi()
        self.completions = _Completions()
        self.chat = SimpleNamespace(completions=self.completions)
        self.responses = _Responses(error=responses_error)
        self.raw_posts: list[tuple[str, dict]] = []

    def with_options(self, **_kwargs):
        return self

    async def get(self, path, **_kwargs):
        if path == "/version":
            return {"version": "0.test"}
        if path == "/openapi.json":
            return self.openapi
        raise AssertionError(path)

    async def post(self, path, *, body, **_kwargs):
        self.raw_posts.append((path, body))
        return {"count": 321, "max_model_len": 131072, "tokens": [1, 2]}


def _request() -> ModelRequest:
    return ModelRequest(
        instructions="Be useful.",
        items=(
            ModelUserInput(source_id="u1", content="first"),
            ModelAssistantTurn(
                source_id="a1",
                content="checking",
                reasoning="private trace",
                tool_calls=(
                    ToolCall(id="call_1", name="lookup", arguments={"query": "x"}),
                ),
            ),
            ModelToolResultInput(
                source_id="t1",
                call_id="call_1",
                name="lookup",
                content="result",
            ),
            ModelUserInput(source_id="u2", content="continue"),
        ),
        tools=(
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look up data.",
                    "parameters": {"type": "object"},
                },
            },
        ),
    )


def _simple_request() -> ModelRequest:
    return ModelRequest(
        instructions="Be useful.",
        items=(ModelUserInput(source_id="u1", content="hello"),),
    )


@pytest.mark.asyncio
async def test_auto_prefers_stateless_responses_and_replays_items() -> None:
    client = DualProtocolClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            api_key="local",
            reasoning_effort="high",
        ),
        client=client,  # type: ignore[arg-type]
    )

    capabilities = await provider.inspect_capabilities()
    response = await provider.complete(_request())

    assert capabilities.server_kind == "vllm"
    assert capabilities.resolved_protocol == "responses"
    assert not client.completions.calls
    kwargs = client.responses.calls[0]
    assert kwargs["store"] is False
    assert "previous_response_id" not in kwargs
    assert kwargs["reasoning"] == {"effort": "high"}
    assert kwargs["extra_body"]["include_reasoning"] is True
    assert [item["type"] for item in kwargs["input"] if "type" in item] == [
        "reasoning",
        "message",
        "function_call",
        "function_call_output",
    ]
    assert kwargs["input"][1]["content"][0]["text"] == "private trace"
    assert kwargs["tools"][0]["name"] == "lookup"
    assert response.content == "response answer"
    assert response.reasoning == "response reasoning"
    assert response.tool_calls[0].id == "call_next"
    assert response.metadata is not None
    assert response.metadata.protocol == "responses"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_compaction_request_overrides_reasoning_effort_and_output_limit(
    protocol: str,
) -> None:
    client = DualProtocolClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            api_key="local",
            protocol=protocol,
            reasoning_effort="high",
            max_output_tokens=4096,
        ),
        client=client,  # type: ignore[arg-type]
    )
    request = replace(
        _simple_request(),
        reasoning_effort="low",
        max_output_tokens=512,
        purpose="compaction",
    )

    response = await provider.complete(request)

    kwargs = (
        client.completions.calls[0]
        if protocol == "chat_completions"
        else client.responses.calls[0]
    )
    if protocol == "chat_completions":
        assert kwargs["reasoning_effort"] == "low"
        assert kwargs["max_completion_tokens"] == 512
    else:
        assert kwargs["reasoning"] == {"effort": "low"}
        assert kwargs["max_output_tokens"] == 512
    assert response.metadata is not None
    assert response.metadata.request_purpose == "compaction"
    assert response.metadata.requested_reasoning_effort == "low"
    assert response.metadata.effective_reasoning_effort == "low"
    assert response.metadata.effective_max_output_tokens == 512


@pytest.mark.asyncio
async def test_unset_output_limit_is_not_sent() -> None:
    client = DualProtocolClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            protocol="responses",
        ),
        client=client,  # type: ignore[arg-type]
    )

    await provider.complete(_simple_request())

    assert "max_output_tokens" not in client.responses.calls[0]


@pytest.mark.asyncio
async def test_explicit_unset_output_limit_does_not_inherit_provider_limit() -> None:
    client = DualProtocolClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            protocol="responses",
            max_output_tokens=512,
        ),
        client=client,  # type: ignore[arg-type]
    )
    request = replace(
        _simple_request(),
        max_output_tokens=None,
        inherit_provider_max_output_tokens=False,
    )

    response = await provider.complete(request)

    assert "max_output_tokens" not in client.responses.calls[0]
    assert response.metadata is not None
    assert response.metadata.requested_max_output_tokens is None
    assert response.metadata.effective_max_output_tokens is None


@pytest.mark.asyncio
async def test_auto_uses_chat_when_only_chat_supports_output_limit() -> None:
    client = DualProtocolClient(openapi=_openapi(responses_output=False))
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="deepseek",
            api_key="local",
            reasoning_effort="medium",
            max_output_tokens=512,
        ),
        client=client,  # type: ignore[arg-type]
    )

    response = await provider.complete(_request())

    assert not client.responses.calls
    kwargs = client.completions.calls[0]
    assert kwargs["reasoning_effort"] == "medium"
    assert kwargs["max_completion_tokens"] == 512
    assistant = next(message for message in kwargs["messages"] if message["role"] == "assistant")
    assert assistant["reasoning"] == "private trace"
    assert response.metadata is not None
    assert response.metadata.protocol == "chat_completions"
    assert response.metadata.effective_max_output_tokens == 512
    assert response.metadata.output_limit_field == "max_completion_tokens"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("missing_field", "model_request"),
    [
        ("tools", _request()),
        ("reasoning", _request()),
        (
            "text",
            replace(
                _simple_request(),
                response_format=StructuredOutputFormat(
                    name="answer",
                    schema={"type": "object"},
                ),
            ),
        ),
    ],
)
async def test_auto_uses_chat_when_responses_lacks_request_capability(
    missing_field: str,
    model_request: ModelRequest,
) -> None:
    spec = _openapi()
    response_properties = spec["paths"]["/v1/responses"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]["properties"]
    response_properties.pop(missing_field)
    client = DualProtocolClient(openapi=spec)
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
        ),
        client=client,  # type: ignore[arg-type]
    )

    response = await provider.complete(model_request)

    assert not client.responses.calls
    assert client.completions.calls
    assert response.metadata is not None
    assert response.metadata.protocol == "chat_completions"
    assert response.metadata.fallback_reason is not None
    assert (
        missing_field.replace("text", "structured_output")
        in response.metadata.fallback_reason
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "auto"])
async def test_chat_omit_does_not_require_reasoning_replay_support(
    protocol: str,
) -> None:
    spec = _openapi()
    spec["components"]["schemas"]["ChatRequest"]["properties"].pop(
        "include_reasoning"
    )
    if protocol == "auto":
        spec["paths"]["/v1/responses"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]["properties"].pop("tools")
    client = DualProtocolClient(openapi=spec)
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            protocol=protocol,  # type: ignore[arg-type]
            chat_reasoning_field="omit",
        ),
        client=client,  # type: ignore[arg-type]
    )

    response = await provider.complete(_request())

    assert not client.responses.calls
    assistant = next(
        message
        for message in client.completions.calls[0]["messages"]
        if message["role"] == "assistant"
    )
    assert "reasoning" not in assistant
    assert "reasoning_content" not in assistant
    assert response.metadata is not None
    assert response.metadata.protocol == "chat_completions"


@pytest.mark.asyncio
async def test_auto_uses_chat_stream_when_responses_lacks_streaming() -> None:
    spec = _openapi()
    response_properties = spec["paths"]["/v1/responses"]["post"]["requestBody"][
        "content"
    ]["application/json"]["schema"]["properties"]
    response_properties.pop("stream")
    client = DualProtocolClient(openapi=spec)
    provider = OpenAICompatibleProvider(
        ProviderConfig(base_url="http://localhost:8000/v1", model="qwen3"),
        client=client,  # type: ignore[arg-type]
    )

    events = [event async for event in provider.stream(_simple_request())]

    assert not client.responses.calls
    assert client.completions.calls[0]["stream"] is True
    assert events[-1].response is not None
    assert events[-1].response.metadata is not None
    assert events[-1].response.metadata.protocol == "chat_completions"


@pytest.mark.asyncio
async def test_auto_rejects_request_when_neither_protocol_supports_it() -> None:
    spec = _openapi()
    spec["paths"]["/v1/responses"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"].pop("tools")
    spec["components"]["schemas"]["ChatRequest"]["properties"].pop("tools")
    client = DualProtocolClient(openapi=spec)
    provider = OpenAICompatibleProvider(
        ProviderConfig(base_url="http://localhost:8000/v1", model="qwen3"),
        client=client,  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderCapabilityError, match="tools"):
        await provider.complete(_request())

    assert not client.responses.calls
    assert not client.completions.calls


@pytest.mark.asyncio
async def test_explicit_protocol_rejects_unsupported_output_limit() -> None:
    spec = _openapi(responses_output=False)
    client = DualProtocolClient(openapi=spec)
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="glm",
            api_key="local",
            protocol="responses",
            max_output_tokens=256,
        ),
        client=client,  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderCapabilityError, match="output_limit"):
        await provider.complete(_request())

    assert not client.responses.calls


@pytest.mark.asyncio
async def test_vllm_tokenize_is_exact_and_caps_discovered_window() -> None:
    client = DualProtocolClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            api_key="local",
            context_window_tokens=65536,
        ),
        client=client,  # type: ignore[arg-type]
    )

    count = await provider.count_tokens(_request())

    assert count is not None
    assert count.count == 321
    assert count.max_model_len == 131072
    assert provider.context_window_tokens == 65536
    assert client.raw_posts[0][0] == "/tokenize"
    assistant = next(
        message
        for message in client.raw_posts[0][1]["messages"]
        if message["role"] == "assistant"
    )
    assert assistant["reasoning"] == "private trace"


@pytest.mark.asyncio
async def test_vllm_tokenize_sets_automatic_window() -> None:
    client = DualProtocolClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
        ),
        client=client,  # type: ignore[arg-type]
    )

    await provider.count_tokens(_simple_request())

    assert provider.configured_context_window_tokens is None
    assert provider.context_window_tokens == 131072


@pytest.mark.asyncio
async def test_explicit_context_window_cannot_exceed_vllm_limit() -> None:
    client = DualProtocolClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            context_window_tokens=262144,
        ),
        client=client,  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderCapabilityError, match="262144.*131072.*qwen3"):
        await provider.count_tokens(_simple_request())


@pytest.mark.asyncio
async def test_tokenize_failure_uses_automatic_fallback_window() -> None:
    client = DualProtocolClient()

    async def failing_post(*_args, **_kwargs):
        raise OSError("tokenizer offline")

    client.post = failing_post  # type: ignore[method-assign]
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
        ),
        client=client,  # type: ignore[arg-type]
    )

    with pytest.warns(ProviderCapabilityWarning, match="32,768-token fallback"):
        count = await provider.count_tokens(_simple_request())

    assert count is None
    assert provider.context_window_tokens == 32768


@pytest.mark.asyncio
async def test_explicit_responses_failure_never_retries_chat() -> None:
    client = DualProtocolClient(responses_error=RuntimeError("responses failed"))
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            api_key="local",
            protocol="responses",
        ),
        client=client,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="responses failed"):
        await provider.complete(_request())

    assert len(client.responses.calls) == 1
    assert not client.completions.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "incomplete"])
async def test_responses_non_success_status_raises(status: str) -> None:
    client = DualProtocolClient()

    async def create(**kwargs):
        client.responses.calls.append(kwargs)
        return SimpleNamespace(
            status=status,
            error=SimpleNamespace(message="generation stopped"),
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            output=[],
            usage=None,
        )

    client.responses.create = create
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            protocol="responses",
        ),
        client=client,  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderResponseError, match=status):
        await provider.complete(_simple_request())

    assert not client.completions.calls


@pytest.mark.asyncio
async def test_responses_failed_stream_never_emits_done() -> None:
    client = DualProtocolClient()
    failed_response = SimpleNamespace(
        status="failed",
        error=SimpleNamespace(message="engine failed"),
        output=[],
        usage=None,
    )

    async def create(**kwargs):
        client.responses.calls.append(kwargs)
        return _AsyncStream(
            [SimpleNamespace(type="response.failed", response=failed_response)]
        )

    client.responses.create = create
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            protocol="responses",
        ),
        client=client,  # type: ignore[arg-type]
    )
    events = []

    with pytest.raises(ProviderResponseError, match="failed"):
        async for event in provider.stream(_simple_request()):
            events.append(event)

    assert not any(event.type == "done" for event in events)


@pytest.mark.asyncio
async def test_chat_length_finish_reason_raises_incomplete() -> None:
    client = DualProtocolClient()

    async def create(**kwargs):
        client.completions.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="length",
                    message=SimpleNamespace(content="partial", tool_calls=[]),
                )
            ],
            usage=None,
        )

    client.completions.create = create
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            protocol="chat_completions",
        ),
        client=client,  # type: ignore[arg-type]
    )

    with pytest.raises(ProviderResponseError, match="incomplete") as error:
        await provider.complete(_simple_request())

    assert error.value.details["finish_reason"] == "length"


@pytest.mark.asyncio
async def test_chat_length_stream_never_emits_done() -> None:
    client = DualProtocolClient()

    async def create(**kwargs):
        client.completions.calls.append(kwargs)
        return _AsyncStream(
            [
                SimpleNamespace(
                    usage=None,
                    choices=[
                        SimpleNamespace(
                            finish_reason=None,
                            delta=SimpleNamespace(
                                content="partial",
                                reasoning=None,
                                refusal=None,
                                tool_calls=[],
                            ),
                        )
                    ],
                ),
                SimpleNamespace(
                    usage=None,
                    choices=[
                        SimpleNamespace(
                            finish_reason="length",
                            delta=SimpleNamespace(
                                content=None,
                                reasoning=None,
                                refusal=None,
                                tool_calls=[],
                            ),
                        )
                    ],
                ),
            ]
        )

    client.completions.create = create
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            protocol="chat_completions",
        ),
        client=client,  # type: ignore[arg-type]
    )
    events = []

    with pytest.raises(ProviderResponseError, match="incomplete"):
        async for event in provider.stream(_simple_request()):
            events.append(event)

    assert "".join(event.content for event in events) == "partial"
    assert not any(event.type == "done" for event in events)


@pytest.mark.asyncio
async def test_responses_stream_normalizes_reasoning_content_and_tool_calls() -> None:
    client = DualProtocolClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            api_key="local",
            protocol="responses",
        ),
        client=client,  # type: ignore[arg-type]
    )

    events = [event async for event in provider.stream(_request())]

    assert [(event.channel, event.content) for event in events[:-1]] == [
        ("reasoning", "stream thought"),
        ("content", "stream answer"),
    ]
    response = events[-1].response
    assert response is not None
    assert response.content == "stream answer"
    assert response.reasoning == "stream thought"
    assert response.tool_calls[0].id == "call_stream"
    assert response.tool_calls[0].arguments == {"query": "stream"}


@pytest.mark.asyncio
async def test_capability_and_tokenize_probes_use_vllm_root_with_real_sdk() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=_openapi())
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "test"})
        if request.url.path == "/tokenize":
            return httpx.Response(
                200,
                json={"count": 42, "max_model_len": 65536, "tokens": [1]},
            )
        return httpx.Response(404, json={"detail": "not found"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AsyncOpenAI(
        api_key="local",
        base_url="http://vllm.test/v1",
        http_client=http_client,
    )
    provider = OpenAICompatibleProvider(
        ProviderConfig(base_url="http://vllm.test/v1", model="qwen3"),
        client=client,
    )
    try:
        capabilities = await provider.inspect_capabilities()
        count = await provider.count_tokens(_request())
    finally:
        await http_client.aclose()

    assert capabilities.server_kind == "vllm"
    assert count is not None and count.count == 42
    assert paths == ["/openapi.json", "/version", "/tokenize"]
