"""Reasoning replay assertions at the actual HTTP serialization boundary."""

import json

import httpx
from openai import AsyncOpenAI
import pytest

import dagent
from dagent.config import ProviderConfig
from dagent.harness_runtime.context import ContextAssembler
from dagent.providers import OpenAICompatibleProvider
from dagent.providers.capabilities import ProviderCapabilities
from dagent.schemas.conversation import inline_content


def capabilities(kind="unknown"):
    return ProviderCapabilities(
        server_kind=kind, resolved_protocol="chat_completions", resolution_reason="test"
    )


@pytest.mark.parametrize("base_url", [
    "https://api.deepseek.com", "https://api.deepseek.com/v1/",
    "https://api.deepseek.com/beta", "https://api.deepseek.com:443/v1",
])
@pytest.mark.parametrize("model", [
    "deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp",
])
def test_official_mapping(base_url, model):
    provider = OpenAICompatibleProvider(ProviderConfig(base_url=base_url, model=model))
    assert provider._resolved_chat_reasoning_field(capabilities()) == "reasoning_content"


@pytest.mark.parametrize("base_url,model", [
    ("https://proxy.example/v1", "deepseek-v4-flash"),
    ("https://api.deepseek.com.example/v1", "deepseek-v4-flash"),
    ("https://api.deepseek.com@proxy.example/v1", "deepseek-v4-flash"),
    ("http://api.deepseek.com", "deepseek-v4-flash"),
    ("https://api.deepseek.com:8443/v1", "deepseek-v4-flash"),
    ("https://api.deepseek.com/custom", "deepseek-v4-flash"),
    ("https://api.deepseek.com", "unknown-future-model"),
])
def test_unknown_protocol_is_conservative(base_url, model):
    provider = OpenAICompatibleProvider(ProviderConfig(base_url=base_url, model=model))
    assert provider._resolved_chat_reasoning_field(capabilities()) == "omit"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,field,expected", [
    ("active_run", "auto", "reasoning_content"),
    ("none", "auto", None),
    ("active_run", "omit", None),
    ("active_run", "reasoning", "reasoning"),
    ("active_run", "reasoning_content", "reasoning_content"),
])
@pytest.mark.parametrize("stream", [False, True])
async def test_run_tool_exchange_serializes_reasoning(tmp_path, mode, field, expected, stream):
    bodies = []
    reasoning = "中文🙂x" * 40

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        first = len(bodies) == 1
        message = {"role": "assistant", "content": "" if first else "done"}
        if first:
            message.update(reasoning_content=reasoning, tool_calls=[{
                "id": "call_1", "type": "function",
                "function": {"name": "tool_lookup", "arguments": "{}"},
            }])
        finish = "tool_calls" if first else "stop"
        if stream:
            if first:
                message["tool_calls"][0]["index"] = 0
            chunk = {"id": "chat_1", "object": "chat.completion.chunk", "created": 0,
                     "model": "deepseek-v4-flash", "choices": [
                         {"index": 0, "delta": message, "finish_reason": finish}
                     ]}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json={
            "id": "chat_1", "object": "chat.completion", "created": 0,
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        })

    @dagent.tool
    def lookup() -> str:
        """Look up a synthetic value."""
        return "42"

    async with AsyncOpenAI(
        api_key="synthetic-key", base_url="https://api.deepseek.com", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as client:
        provider = OpenAICompatibleProvider(ProviderConfig(
            base_url="https://api.deepseek.com", model="deepseek-v4-flash",
            protocol="chat_completions", chat_reasoning_field=field,
        ), client=client)
        provider._capabilities = capabilities()
        runner = dagent.Runner(provider=provider, workspace=tmp_path,
                               capabilities=[lookup], skill_roots=[])
        agent = dagent.ToolAgent(profile="conversation", capabilities=["tool.lookup"], max_steps=2,
                                 context=dagent.ContextPolicy(reasoning_replay=mode))
        try:
            if stream:
                events = [event async for event in runner.stream(agent, input="Look up the value.")]
                assert events[-1].type == "run.finished"
            else:
                result = await runner.run(agent, input="Look up the value.")
                assert result.output_text == "done"
        finally:
            runner.close()
    assert len(bodies) == 2
    assistant = next(m for m in bodies[1]["messages"] if m["role"] == "assistant")
    for name in ("reasoning", "reasoning_content"):
        if name == expected:
            assert assistant[name] == reasoning
        else:
            assert name not in assistant


@pytest.mark.asyncio
async def test_active_run_does_not_serialize_other_runs():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "chat_1", "object": "chat.completion",
            "created": 0, "model": "deepseek-v4-flash", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "done"},
                 "finish_reason": "stop"}]})

    conversation = dagent.ConversationState(items=(
        dagent.UserMessage(run_id="old", content="Earlier question"),
        dagent.AssistantMessage(run_id="old", content="Earlier answer", reasoning="OLD"),
        dagent.UserMessage(run_id="current", content="Current question"),
        dagent.AssistantMessage(run_id="current", content="Considering", reasoning="PLAIN"),
        dagent.AssistantMessage(run_id="current", reasoning="CURRENT", tool_calls=(
            dagent.ToolCallItem(id="call", name="lookup"),)),
        dagent.ToolResultMessage(run_id="current", call_id="call", name="lookup",
                                status="completed", content=inline_content("42")),
    ))
    prepared = await ContextAssembler().prepare(
        system_message={"content": "test"}, conversation=conversation,
        policy=dagent.ContextPolicy(), active_run_id="current",
    )
    async with AsyncOpenAI(api_key="synthetic-key", base_url="https://api.deepseek.com",
            max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
        provider = OpenAICompatibleProvider(ProviderConfig(
            base_url="https://api.deepseek.com", model="deepseek-v4-flash",
            protocol="chat_completions"), client=client)
        provider._capabilities = capabilities()
        await provider.complete(prepared.request)
    assistants = [m for m in bodies[0]["messages"] if m["role"] == "assistant"]
    assert "reasoning_content" not in assistants[0]
    assert assistants[1]["reasoning_content"] == "PLAIN"
    assert assistants[2]["reasoning_content"] == "CURRENT"
