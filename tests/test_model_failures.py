"""Terminal model responses must preserve the run and never replay tools."""

from contextlib import closing
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI

import dagent
from dagent.config import ProviderConfig
from dagent.harness_runtime.llm_retry import LLMRetryPolicy, run_with_llm_retries
from dagent.harness_runtime import tool_agent as tool_runtime
from dagent.harness_runtime import profiled_agent as profiled_runtime
from dagent.providers import (
    ChatResponse, ChatStreamEvent, MockProvider, OpenAICompatibleProvider,
    ProviderResponseError, ProviderTokenCountError, ToolCall,
)
from dagent.schemas import ToolResultMessage
from tests.test_model_protocols import DualProtocolClient, _AsyncStream, _simple_request


async def _run(runner, agent, *, streaming):
    if streaming:
        events = [event async for event in runner.stream(agent, input="Find a record")]
        return events[-1].data.result
    return await runner.run(agent, input="Find a record")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("final", ["answer", "reasoning", "tags", "empty", "whitespace", "refusal"])
async def test_tool_failure_then_model_response_retains_evidence(tmp_path, streaming, final):
    executed = []

    @dagent.tool
    def lookup(key: str) -> str:
        """Look up a record."""
        executed.append(key)
        if key == "bad":
            raise ValueError("record not found")
        return "record found"

    pseudo_call = '<tool_call>{"name":"lookup","arguments":{"key":"good"}}</tool_call>'
    responses = [ChatResponse(tool_calls=[ToolCall(id="bad_call", name="tool_lookup", arguments={"key": "bad"})])]
    if final == "answer":
        responses += [
            ChatResponse(tool_calls=[ToolCall(id="good_call", name="tool_lookup", arguments={"key": "good"})]),
            ChatResponse(content="record found"),
        ]
    else:
        responses.append({
            "reasoning": ChatResponse(reasoning_content=pseudo_call),
            "tags": ChatResponse(content=f"<think>{pseudo_call}</think>"),
            "empty": ChatResponse(),
            "whitespace": ChatResponse(content=" \n ", reasoning_content=" \n "),
            "refusal": ChatResponse(refusal="Cannot comply."),
        }[final])
    provider = MockProvider(responses)
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, capabilities=[lookup])) as runner:
        result = await _run(runner, dagent.ToolAgent(profile="conversation"), streaming=streaming)
        restored = dagent.RunResult.model_validate(result.model_dump(mode="json"))
        assert runner.run_checkpoint(result.run_id).state.status == result.status

    results = [item for item in result.conversation.items if isinstance(item, ToolResultMessage)]
    assert results[0].status == "failed"
    assert "record not found" in str(results[0].content)
    assert provider.requests[1]["messages"][-1]["role"] == "tool"
    assert "record not found" in provider.requests[1]["messages"][-1]["content"]
    assert result.trace.root.children[1].capability_execution.result.status == "failed"
    assert result.checkpoint is not None
    if final in {"answer", "refusal"}:
        assert result.status == "completed"
        assert result.error is None
        assert result.output_text == ("record found" if final == "answer" else "Cannot comply.")
        assert executed == (["bad", "good"] if final == "answer" else ["bad"])
    else:
        assert result.status == "failed"
        assert executed == ["bad"]
        assert len(provider.requests) == 2
        assert result.output_text == ""
        assert result.error.code == ("reasoning_only_response" if final in {"reasoning", "tags"} else "empty_response")
        assert restored.error == result.error
        assert result.trace.root.children[-1].status == "failed"
        assert result.trace.root.children[-1].error == result.error
        if final in {"reasoning", "tags"}:
            assert result.conversation.items[-1].reasoning == pseudo_call
            assert result.trace.root.children[-1].output["reasoning"] == pseudo_call


@pytest.mark.asyncio
async def test_failed_model_response_is_not_reexecuted_by_validator(tmp_path):
    executed = []

    @dagent.tool
    def lookup(key: str) -> str:
        """Look up a record."""
        executed.append(key)
        return "record found"

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="lookup_1", name="tool_lookup", arguments={"key": "good"})]),
        ChatResponse(reasoning_content="Need more work."),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, capabilities=[lookup], validator="validator_agent")) as runner:
        result = await runner.run(dagent.ToolAgent(profile="conversation"), input="Finish this")
    assert result.status == "failed"
    assert len(provider.requests) == 2
    assert executed == ["good"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rate_limit", "timeout"])
async def test_validator_retries_transient_requests_without_replaying_tools(tmp_path, failure, monkeypatch):
    posts = []
    sleeps = []
    executed = []

    @dagent.tool
    def lookup(key: str) -> str:
        """Look up a record."""
        executed.append(key)
        return "record found"

    async def handler(request):
        if request.method == "GET":
            return httpx.Response(404)
        posts.append(json.loads(request.content))
        if len(posts) == 3:
            if failure == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            return httpx.Response(429, json={"error": {"message": "slow down", "type": "rate_limit"}})
        message = {"role": "assistant", "content": "record found"}
        finish_reason = "stop"
        if len(posts) == 1:
            message.update(content=None, tool_calls=[{
                "id": "lookup_1", "type": "function",
                "function": {"name": "tool_lookup", "arguments": '{"key":"good"}'},
            }])
            finish_reason = "tool_calls"
        elif len(posts) == 4:
            message["content"] = '{"passed":true,"issues":[],"summary":"ok"}'
        return httpx.Response(200, json={
            "id": "r", "model": "test", "object": "chat.completion", "created": 1,
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
        })

    async def record_sleep(delay):
        sleeps.append(delay)

    async def bounded_retries(operation, **kwargs):
        return await run_with_llm_retries(operation, **{**kwargs, "policy": LLMRetryPolicy(max_retries=1), "sleep": record_sleep})

    monkeypatch.setattr(profiled_runtime, "run_with_llm_retries", bounded_retries)
    async with AsyncOpenAI(base_url="http://local/v1", api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
        provider = OpenAICompatibleProvider(ProviderConfig(
            base_url="http://local/v1", model="test", protocol="chat_completions",
            token_counting="heuristic", context_window_tokens=131072,
        ), client=client)
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, capabilities=[lookup], validator="validator_agent")) as runner:
            with pytest.warns(RuntimeWarning, match="discovery failed"):
                result = await runner.run(dagent.ToolAgent(profile="conversation"), input="Find a record")

    assert result.status == "completed"
    assert result.output_text == "record found"
    assert executed == ["good"]
    assert len(posts) == 4
    assert posts[2] == posts[3]
    assert sleeps == [1.0]
    audit = next(item for item in result.new_items if item.type == "assistant" and item.scope == "validator")
    assert [attempt.attempt for attempt in audit.model_call.attempts] == [1, 2]
    assert audit.model_call.attempts[0].exception_type == ("RateLimitError" if failure == "rate_limit" else "APITimeoutError")
    assert audit.model_call.attempts[-1].http_status == 200


@pytest.mark.asyncio
async def test_custom_stream_without_done_preserves_partial_response(tmp_path):
    class MissingDoneProvider(MockProvider):
        async def stream_chat(self, messages, tools=None, *, response_format=None):
            yield ChatStreamEvent(type="token", content="partial answer")

    with closing(dagent.Runner(provider=MissingDoneProvider([]), workspace=tmp_path)) as runner:
        result = await _run(runner, dagent.ToolAgent(profile="conversation"), streaming=True)
    assert result.status == "failed"
    assert result.error.code == "missing_terminal_response"
    assert result.conversation.items[-1].content == "partial answer"
    assert result.output_text == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("finish,reason", [("length", "max_output_tokens"), (None, "missing_finish_reason"), ("error", "invalid_finish_reason")])
async def test_chat_protocol_failure_preserves_response_and_usage(streaming, finish, reason):
    client = DualProtocolClient()
    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5)
    message = SimpleNamespace(content="partial answer", reasoning_content="thought", tool_calls=[])

    async def create(**kwargs):
        if streaming:
            return _AsyncStream([SimpleNamespace(usage=usage, choices=[SimpleNamespace(delta=message, finish_reason=finish)])])
        return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=message, finish_reason=finish)])

    client.completions.create = create
    provider = OpenAICompatibleProvider(ProviderConfig(base_url="http://local/v1", model="test", protocol="chat_completions"), client=client)
    with pytest.raises(ProviderResponseError) as caught:
        if streaming:
            [event async for event in provider.stream(_simple_request())]
        else:
            await provider.complete(_simple_request())
    error = caught.value
    assert error.reason == reason
    assert error.response.content == "partial answer"
    assert error.response.reasoning == "thought"
    assert error.response.usage.total_tokens == 5
    assert error.response.usage.reasoning_tokens is None
    assert error.response.metadata.finish_reason == finish
    assert error.response.metadata.response_status is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_output_exhaustion_retains_prior_tools_and_truncated_arguments(tmp_path, streaming):
    calls = 0
    executed = []

    @dagent.tool
    def lookup(key: str) -> str:
        """Look up a record."""
        executed.append(key)
        return "saved record"

    client = DualProtocolClient()

    async def create(**kwargs):
        nonlocal calls
        calls += 1
        finish = "tool_calls" if calls == 1 else "length"
        arguments = '{"key":"first"}' if calls == 1 else '{"key":'
        function = SimpleNamespace(name="tool_lookup", arguments=arguments)
        message = SimpleNamespace(
            content="" if calls == 1 else "unfinished answer", reasoning_content="thought",
            tool_calls=[SimpleNamespace(id=f"call_{calls}", index=0, function=function)],
        )
        usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5)
        if streaming:
            return _AsyncStream([SimpleNamespace(usage=usage, choices=[SimpleNamespace(delta=message, finish_reason=finish)])])
        return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=message, finish_reason=finish)])

    client.completions.create = create
    provider = OpenAICompatibleProvider(ProviderConfig(base_url="http://local/v1", model="test", protocol="chat_completions", token_counting="heuristic"), client=client)
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, capabilities=[lookup])) as runner:
        result = await _run(runner, dagent.ToolAgent(profile="conversation"), streaming=streaming)
    assert result.status == "failed"
    assert result.error.code == "max_output_tokens"
    assert result.output_text == ""
    assert calls == 2
    assert executed == ["first"]
    assert result.trace.root.children[1].capability_execution.result.content == "saved record"
    last = result.conversation.items[-1]
    assert last.content == "unfinished answer"
    assert last.reasoning == "thought"
    assert last.usage.total_tokens == 5
    assert last.model_call.finish_reason == "length"
    assert result.trace.root.children[-1].output["provider_details"]["tool_calls"][0]["arguments"] == '{"key":'


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_interrupted_http_stream_retains_all_received_text(tmp_path, protocol):
    posts = 0
    text = "a partial answer still in the parser buffer"

    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if protocol == "chat_completions":
                event = {"id": "r", "model": "test", "object": "chat.completion.chunk", "created": 1,
                         "choices": [{"index": 0, "finish_reason": None, "delta": {"content": text}}]}
            else:
                event = {"type": "response.output_text.delta", "item_id": "m", "output_index": 0,
                         "content_index": 0, "delta": text, "sequence_number": 1}
            yield f"data: {json.dumps(event)}\n\n".encode()
            raise httpx.ReadTimeout("stream timed out")

    async def handler(request):
        nonlocal posts
        if request.method == "GET":
            return httpx.Response(404)
        posts += 1
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=InterruptedStream())

    async with AsyncOpenAI(base_url="http://local/v1", api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
        provider = OpenAICompatibleProvider(ProviderConfig(base_url="http://local/v1", model="test", protocol=protocol, token_counting="heuristic"), client=client)
        with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
            with pytest.warns(RuntimeWarning, match="discovery failed"):
                result = await _run(runner, dagent.ToolAgent(profile="conversation"), streaming=True)
    assert posts == 1
    assert result.status == "failed"
    assert result.error.code == "provider_request_failed"
    assert result.output_text == ""
    last = result.conversation.items[-1]
    assert last.content == text
    assert last.model_call.finish_reason is None
    assert last.model_call.response_status is None
    assert last.model_call.attempts[0].exception_type == "ReadTimeout"
    assert last.model_call.attempts[0].http_status == 200
    assert last.model_call.attempts[0].retry_delay_seconds is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "failed", "incomplete", "cancelled"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_responses_requires_terminal_completion(streaming, status):
    client = DualProtocolClient()

    async def create(**kwargs):
        response = SimpleNamespace(status=status, output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text="partial")])])
        if streaming:
            return _AsyncStream([
                SimpleNamespace(type="response.output_text.delta", delta="partial"),
                *([SimpleNamespace(type=f"response.{status}", response=response)] if status else []),
            ])
        return response

    client.responses.create = create
    provider = OpenAICompatibleProvider(ProviderConfig(base_url="http://local/v1", model="test", protocol="responses"), client=client)
    with pytest.raises(ProviderResponseError) as caught:
        if streaming:
            [event async for event in provider.stream(_simple_request())]
        else:
            await provider.complete(_simple_request())
    assert caught.value.response.content == "partial"
    assert caught.value.response.metadata.response_status == status
    assert caught.value.response.usage is None


@pytest.mark.asyncio
async def test_responses_failed_terminal_retains_preceding_deltas():
    client = DualProtocolClient()

    async def create(**kwargs):
        return _AsyncStream([
            SimpleNamespace(type="response.output_text.delta", delta="received text"),
            SimpleNamespace(type="response.reasoning_text.delta", delta="received reasoning"),
            SimpleNamespace(type="response.failed", response=SimpleNamespace(status="failed", output=[])),
        ])

    client.responses.create = create
    provider = OpenAICompatibleProvider(ProviderConfig(base_url="http://local/v1", model="test", protocol="responses"), client=client)
    with pytest.raises(ProviderResponseError) as caught:
        [event async for event in provider.stream(_simple_request())]
    assert caught.value.response.content == "received text"
    assert caught.value.response.reasoning == "received reasoning"
    assert caught.value.response.metadata.response_status == "failed"


@pytest.mark.asyncio
async def test_missing_usage_fields_are_unknown_and_reported_zero_is_preserved():
    client = DualProtocolClient()

    async def create(**kwargs):
        return SimpleNamespace(
            status="completed", output=[], usage=SimpleNamespace(output_tokens=0),
        )

    client.responses.create = create
    provider = OpenAICompatibleProvider(ProviderConfig(base_url="http://local/v1", model="test", protocol="responses"), client=client)
    response = await provider.complete(_simple_request())
    assert response.usage.model_dump() == {
        "input_tokens": None, "output_tokens": 0, "reasoning_tokens": None, "total_tokens": None,
    }


@pytest.mark.asyncio
async def test_token_probe_failure_keeps_verified_server_limit(tmp_path):
    client = DualProtocolClient()
    payload = {"count": 4, "max_model_len": 65536}

    async def post(*args, **kwargs):
        return payload

    client.post = post
    provider = OpenAICompatibleProvider(ProviderConfig(base_url="http://local/v1", model="test", protocol="chat_completions"), client=client)
    await provider.count_tokens(_simple_request())
    payload = {"count": 0}
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        with pytest.warns(RuntimeWarning, match="last verified server context window"):
            result = await runner.run(dagent.ToolAgent(profile="conversation"), input="Hello")
    usage = result.context_usage[0]
    assert usage.context_window_tokens == 65536
    assert usage.server_max_model_len == 65536
    assert usage.context_window_source == "server"
    assert usage.estimator == "heuristic"
    assert provider.context_window_source == "server"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {}, [], {"tokens": []}, {"tokens": "bad"}, {"tokens": ["bad"]},
    {"count": 0}, {"count": False}, {"count": -1}, {"count": "3"}, {"count": 1.5},
    {"count": 4, "max_model_len": 0}, {"count": 4, "max_model_len": "131072"},
])
@pytest.mark.parametrize("mode", ["auto", "vllm"])
async def test_invalid_token_counts_are_never_exact_zero(tmp_path, payload, mode):
    client = DualProtocolClient()

    async def post(*args, **kwargs):
        return payload

    client.post = post
    provider = OpenAICompatibleProvider(ProviderConfig(base_url="http://local/v1", model="test", token_counting=mode, protocol="chat_completions"), client=client)
    if mode == "vllm":
        with pytest.raises(ProviderTokenCountError):
            await provider.count_tokens(_simple_request())
    else:
        with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
            with pytest.warns(RuntimeWarning, match="heuristic counting"):
                result = await runner.run(dagent.ToolAgent(profile="conversation"), input="Hello")
        usage = result.context_usage[0]
        assert usage.estimated_input_tokens > 0
        assert usage.estimator == "heuristic"
        assert usage.server_max_model_len is None
        assert usage.context_window_source == "fallback"
        assert provider.context_window_source == "fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rate_limit", "timeout"])
async def test_retry_attempts_are_public_and_sdk_retries_are_disabled(tmp_path, failure, monkeypatch):
    posts = 0
    sleeps = []

    async def handler(request):
        nonlocal posts
        if request.method == "GET":
            return httpx.Response(404)
        posts += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if posts == 1:
            return httpx.Response(429, json={"error": {"message": "slow down", "type": "rate_limit"}})
        return httpx.Response(200, json={"id": "r", "model": "test", "object": "chat.completion", "created": 1, "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}]})

    async def record_sleep(delay):
        sleeps.append(delay)

    original_retries = tool_runtime.run_with_llm_retries

    async def bounded_retries(operation, **kwargs):
        return await original_retries(
            operation, **{**kwargs, "policy": LLMRetryPolicy(max_retries=1), "sleep": record_sleep},
        )

    monkeypatch.setattr(tool_runtime, "run_with_llm_retries", bounded_retries)

    async with AsyncOpenAI(base_url="http://local/v1", api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
        provider = OpenAICompatibleProvider(ProviderConfig(base_url="http://local/v1", model="test", protocol="chat_completions", token_counting="heuristic"), client=client)
        with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
            with pytest.warns(RuntimeWarning, match="discovery failed"):
                result = await runner.run(dagent.ToolAgent(profile="conversation"), input="Hello")
    assert posts == 2
    assert sleeps == [1.0]
    attempts = result.conversation.items[-1].model_call.attempts
    assert [attempt.attempt for attempt in attempts] == [1, 2]
    assert all(attempt.elapsed_seconds >= 0 for attempt in attempts)
    assert attempts[0].retry_delay_seconds == 1.0
    assert attempts[-1].retry_delay_seconds is None
    if failure == "rate_limit":
        assert result.status == "completed"
        assert [attempt.http_status for attempt in attempts] == [429, 200]
        assert attempts[0].exception_type == "RateLimitError"
        assert attempts[1].exception_type is None
    else:
        assert result.status == "failed"
        assert result.error.code == "provider_request_failed"
        assert all(attempt.exception_type == "APITimeoutError" for attempt in attempts)
        assert all(attempt.http_status is None for attempt in attempts)
