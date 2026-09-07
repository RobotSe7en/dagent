import asyncio
import json

import httpx
from openai import AsyncOpenAI
import pytest

import dagent
from dagent.config import ProviderConfig
from dagent.providers import OpenAICompatibleProvider
from dagent.providers.capabilities import ProviderCapabilities
from dagent.providers.model_io import ModelAssistantTurn, ModelRequest
from dagent.schemas.context import ModelCallMetadata, RequestReasoning


def completion_body(protocol):
    if protocol == "responses":
        return {"id": "resp_1", "object": "response", "created_at": 0,
                "model": "test", "status": "completed", "output": [{
                    "type": "message", "id": "msg_1", "role": "assistant",
                    "status": "completed", "content": [
                        {"type": "output_text", "text": "done", "annotations": []}]}]}
    return {"id": "chat_1", "object": "chat.completion", "created": 0,
            "model": "test", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "done"}}]}


def completion_response(protocol, stream):
    response = completion_body(protocol)
    if not stream:
        return httpx.Response(200, json=response)
    if protocol == "responses":
        event = {"type": "response.completed", "response": response, "sequence_number": 1}
    else:
        event = {"id": "chat_1", "object": "chat.completion.chunk", "created": 0,
                 "model": "test", "choices": [{"index": 0, "finish_reason": "stop",
                    "delta": {"content": "done"}}]}
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                         text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol,kind,setting,field", [
    ("chat_completions", "vllm", "auto", "reasoning"),
    ("chat_completions", "unknown", "reasoning_content", "reasoning_content"),
    ("chat_completions", "unknown", "auto", "omit"),
    ("chat_completions", "vllm", "omit", "omit"),
    ("responses", "unknown", "omit", "reasoning"),
])
@pytest.mark.parametrize("stream", [False, True])
async def test_statistics_match_serialized_request(protocol, kind, setting, field, stream):
    bodies = []
    reasoning = '中文🙂\\"\n' * 20

    def handler(request):
        bodies.append(json.loads(request.content))
        return completion_response(protocol, stream)

    async with AsyncOpenAI(api_key="synthetic-key", base_url="https://test.invalid/v1",
            max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
        provider = OpenAICompatibleProvider(ProviderConfig(
            base_url="https://test.invalid/v1", model="test", protocol=protocol,
            chat_reasoning_field=setting), client=client)
        provider._capabilities = ProviderCapabilities(
            server_kind=kind, resolved_protocol=protocol, resolution_reason="test")
        request = ModelRequest(instructions="", items=(
            ModelAssistantTurn(source_id="a", reasoning=reasoning),
            ModelAssistantTurn(source_id="b", reasoning=""),
        ))
        if stream:
            events = [event async for event in provider.stream(request)]
            response = events[-1].response
        else:
            response = await provider.complete(request)
    observation = response.metadata.request_reasoning
    assert observation.resolved_field == field
    assert observation.serialized_fields == (field,)
    assert observation.serialized_items == (0 if field == "omit" else 1)
    assert observation.serialized_characters == (0 if field == "omit" else len(reasoning))
    if protocol == "chat_completions":
        values = [value for message in bodies[0]["messages"]
                  for key, value in message.items() if key in ("reasoning", "reasoning_content")]
    else:
        values = [part["text"] for item in bodies[0]["input"] if item["type"] == "reasoning"
                  for part in item["content"]]
    assert observation.serialized_characters == sum(map(len, values))
    if field == "omit":
        assert observation.omission_reasons == (
            "explicit_omit" if setting == "omit" else "auto_unsupported",
        )
    else:
        assert observation.omission_reasons == ()
    assert reasoning not in response.metadata.model_dump_json()
    assert "synthetic-key" not in response.metadata.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("override_source", ["extra_body", "extra_request_args"])
@pytest.mark.parametrize("replacement", [
    [{"role": "assistant", "content": "replacement"}],
    [{"role": "assistant", "reasoning": "甲", "reasoning_content": "乙🙂"}],
])
async def test_message_overrides_are_observed(override_source, replacement):
    def handler(request):
        assert json.loads(request.content)["messages"] == replacement
        return completion_response("chat_completions", False)

    async with AsyncOpenAI(api_key="synthetic-key", base_url="https://test.invalid/v1",
            max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
        provider = OpenAICompatibleProvider(ProviderConfig(
            base_url="https://test.invalid/v1", model="test", protocol="chat_completions",
            chat_reasoning_field="reasoning_content",
            **{override_source: {"messages": replacement}}), client=client)
        provider._capabilities = ProviderCapabilities(
            resolved_protocol="chat_completions", resolution_reason="test")
        response = await provider.complete(ModelRequest(instructions="", items=(
            ModelAssistantTurn(source_id="a", reasoning="ORIGINAL"),)))
    observation = response.metadata.request_reasoning
    assert observation.resolved_field == "reasoning_content"
    assert observation.omission_reasons == ("request_override",)
    assert observation.serialized_items == (2 if "reasoning" in replacement[0] else 0)
    assert observation.serialized_characters == (3 if "reasoning" in replacement[0] else 0)
    assert observation.serialized_fields == (
        ("reasoning", "reasoning_content") if "reasoning" in replacement[0] else ("omit",))


@pytest.mark.asyncio
async def test_responses_overrides_count_text_items_not_effort():
    def handler(request):
        assert json.loads(request.content)["reasoning"] == {"effort": "low"}
        return completion_response("responses", False)

    async with AsyncOpenAI(api_key="synthetic-key", base_url="https://test.invalid/v1",
            max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
        provider = OpenAICompatibleProvider(ProviderConfig(
            base_url="https://test.invalid/v1", model="test", protocol="responses",
            reasoning_effort="low", extra_body={"input": [
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "中文"}]},
                {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "🙂"},
                                                     {"type": "reasoning_text", "text": "x"}]},
                {"type": "reasoning", "summary": []},
            ]}), client=client)
        provider._capabilities = ProviderCapabilities(resolved_protocol="responses", resolution_reason="test")
        response = await provider.complete(ModelRequest(instructions="", items=()))
    observation = response.metadata.request_reasoning
    assert observation.serialized_fields == ("reasoning",)
    assert observation.serialized_items == 2
    assert observation.serialized_characters == 4


@pytest.mark.asyncio
async def test_concurrent_calls_keep_separate_observations():
    async def handler(request):
        await asyncio.sleep(0)
        return completion_response("chat_completions", False)

    async with AsyncOpenAI(api_key="synthetic-key", base_url="https://test.invalid/v1",
            max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
        provider = OpenAICompatibleProvider(ProviderConfig(
            base_url="https://test.invalid/v1", model="test", protocol="chat_completions",
            chat_reasoning_field="reasoning"), client=client)
        provider._capabilities = ProviderCapabilities(resolved_protocol="chat_completions", resolution_reason="test")
        responses = await asyncio.gather(*(provider.complete(ModelRequest(instructions="", items=(
            ModelAssistantTurn(source_id=str(n), reasoning="文" * n),))) for n in (1, 23)))
    assert [r.metadata.request_reasoning.serialized_characters for r in responses] == [1, 23]


def test_old_metadata_is_unknown_and_new_conversation_round_trips():
    old = ModelCallMetadata.model_validate({"protocol": "chat_completions"})
    assert old.request_reasoning is None
    new = old.model_copy(update={"request_reasoning": RequestReasoning(
        resolved_field="reasoning_content", serialized_fields=("omit",),
        serialized_items=0, serialized_characters=0, omission_reasons=("policy_none",),
    )})
    conversation = dagent.ConversationState(items=(dagent.AssistantMessage(model_call=new),))
    restored = dagent.ConversationState.model_validate_json(conversation.model_dump_json())
    assert restored == conversation
    assert restored.schema_version == 4
    legacy = conversation.model_dump(mode="json")
    del legacy["items"][0]["model_call"]["request_reasoning"]
    assert dagent.ConversationState.model_validate(legacy).items[0].model_call.request_reasoning is None
