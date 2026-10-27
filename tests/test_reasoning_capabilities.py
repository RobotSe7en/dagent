"""Reasoning controls and assistant replay are independent wire capabilities."""

from contextlib import asynccontextmanager
from dataclasses import replace
import json

import httpx
from openai import AsyncOpenAI
import pytest

import dagent
from dagent.providers import ProviderCapabilityError, ProviderRequestError
from dagent.providers.model_io import ModelAssistantTurn, ModelRequest, ModelUserInput
from tests.test_model_protocols import _openapi, _request, _simple_request
from tests.test_request_reasoning import completion_response


def gateway_schema(message=None):
    """The gateway declares effort and open messages, but no response switch."""
    return {
        "openapi": "3.1.0", "info": {"title": "Scheduler", "version": "0.1.0"},
        "paths": {"/v1/chat/completions": {"post": {"requestBody": {"content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/ChatRequest"}},
        }}}}},
        "components": {"schemas": {
            "ChatRequest": {"type": "object", "additionalProperties": True, "properties": {
                "model": {"type": "string"},
                "messages": {"type": "array", "items": {"$ref": "#/components/schemas/ChatMessage"}},
                "reasoning_effort": {"type": "string"}, "enable_thinking": {"type": "boolean"},
                "stream": {"type": "boolean"}, "tools": {"type": "array"},
                "max_tokens": {"type": "integer"},
            }},
            "ChatMessage": message if message is not None else {
                "type": "object", "additionalProperties": True,
                "properties": {"role": {"type": "string"}, "content": {}},
            },
        }},
    }


@asynccontextmanager
async def endpoint(*, spec=None, responder=None, native=False, **options):
    posts = []

    def handle(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=spec if spec is not None else gateway_schema())
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "test"}) if native else httpx.Response(404)
        if request.method == "GET":
            return httpx.Response(404)
        posts.append(request)
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"count": 32, "max_model_len": 32768})
        if request.url.path.endswith("/render"):
            return httpx.Response(200, json={"token_ids": list(range(32)), "max_model_len": 32768})
        if responder is not None:
            return responder(request)
        return completion_response(
            "responses" if request.url.path.endswith("/responses") else "chat_completions",
            json.loads(request.content).get("stream", False),
        )

    async with AsyncOpenAI(
        base_url="http://model.test/v1", api_key="synthetic", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as client:
        yield dagent.Provider(base_url="http://model.test/v1", model="selected", client=client, **options), posts


async def invoke(provider, request, stream):
    if stream:
        return [event async for event in provider.stream(request)][-1].response
    return await provider.complete(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["auto", "chat_completions"])
@pytest.mark.parametrize("stream", [False, True])
async def test_effort_does_not_require_top_level_reasoning(protocol, stream):
    async with endpoint(protocol=protocol, reasoning_effort="medium") as (provider, posts):
        response = await invoke(provider, _simple_request(), stream)
    assert response.content == "done"
    assert len(posts) == 1
    body = json.loads(posts[0].content)
    assert body["reasoning_effort"] == "medium"
    assert "include_reasoning" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["auto", "chat_completions"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("effort", [None, "none", "medium"])
@pytest.mark.parametrize("purpose", ["generation", "compaction"])
async def test_explicit_replay_is_independent_of_effort(protocol, stream, effort, purpose):
    request = replace(_request(), purpose=purpose)
    async with endpoint(protocol=protocol, reasoning_effort=effort,
                        chat_reasoning_field="reasoning_content") as (provider, posts):
        capabilities = await provider.inspect_capabilities()
        response = await invoke(provider, request, stream)
    assert capabilities.chat_completions.replay_reasoning_content == "unknown"
    assert response.content == "done"
    assert "unverified" in response.metadata.fallback_reason.lower()
    assert len(posts) == 1
    body = json.loads(posts[0].content)
    assistant = next(message for message in body["messages"] if message["role"] == "assistant")
    assert assistant["reasoning_content"] == "private trace"
    assert body.get("reasoning_effort") == effort
    assert "include_reasoning" not in body
    assert response.metadata.request_reasoning.serialized_characters == len("private trace")


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
@pytest.mark.parametrize("stream", [False, True])
async def test_declared_message_field_is_supported(field, stream):
    schema = gateway_schema({"type": "object", "properties": {field: {"type": "string"}}})
    async with endpoint(spec=schema, chat_reasoning_field=field) as (provider, posts):
        capabilities = await provider.inspect_capabilities()
        await invoke(provider, _request(), stream)
    assert getattr(capabilities.chat_completions, "replay_" + field) == "supported"
    assert any(message.get(field) == "private trace" for message in json.loads(posts[0].content)["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["auto", "chat_completions"])
@pytest.mark.parametrize("entrypoint", ["complete", "stream", "chat", "stream_chat"])
async def test_closed_message_field_is_rejected_before_post(protocol, entrypoint):
    spec = gateway_schema({"type": "object", "additionalProperties": False,
                           "properties": {"role": {}, "content": {}, "tool_calls": {}}})
    async with endpoint(spec=spec, protocol=protocol,
                        chat_reasoning_field="reasoning_content") as (provider, posts):
        with pytest.raises(ProviderCapabilityError, match="replay_reasoning_content"):
            if entrypoint == "complete":
                await provider.complete(_request())
            elif entrypoint == "stream":
                [event async for event in provider.stream(_request())]
            elif entrypoint == "chat":
                await provider.chat([{"role": "assistant", "content": "previous", "reasoning_content": "trace"}])
            else:
                [event async for event in provider.stream_chat([
                    {"role": "assistant", "content": "previous", "reasoning_content": "trace"},
                ])]
        assert posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_gateway_auto_does_not_guess_replay_field(stream):
    async with endpoint(reasoning_effort="medium") as (provider, posts):
        response = await invoke(provider, _request(), stream)
    body = json.loads(posts[0].content)
    assert all("reasoning" not in message and "reasoning_content" not in message for message in body["messages"])
    assert response.metadata.request_reasoning.resolved_field == "omit"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_include_reasoning_is_only_generated_when_declared(protocol, declared, stream):
    spec = _openapi()
    properties = (spec["components"]["schemas"]["ChatRequest"]["properties"] if protocol == "chat_completions"
                  else spec["paths"]["/v1/responses"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"])
    if declared:
        properties["include_reasoning"] = {"type": "boolean"}
    else:
        properties.pop("include_reasoning", None)
    async with endpoint(spec=spec, native=True, protocol=protocol,
                        reasoning_effort="medium") as (provider, posts):
        await invoke(provider, _simple_request(), stream)
    body = json.loads(posts[0].content)
    assert body.get("include_reasoning") is (True if declared else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"extra_body": {"include_reasoning": False}},
    {"extra_body": {"include_reasoning": True}},
    {"extra_request_args": {"extra_body": {"include_reasoning": False}}},
])
async def test_explicit_include_switch_keeps_precedence(extra):
    async with endpoint(reasoning_effort="medium", **extra) as (provider, posts):
        await provider.complete(_simple_request())
    expected = extra.get("extra_body", extra.get("extra_request_args", {}).get("extra_body"))
    assert json.loads(posts[0].content)["include_reasoning"] is expected["include_reasoning"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 422])
async def test_replay_rejection_does_not_drop_history_or_retry(status):
    async with endpoint(chat_reasoning_field="reasoning_content", responder=lambda _: httpx.Response(
        status, json={"error": {"message": "Replay rejected", "param": "reasoning_content"}},
    )) as (provider, posts):
        with pytest.raises(ProviderRequestError) as captured:
            await provider.complete(_request())
    assert captured.value.status_code == status
    assert len(posts) == 1
    assert any(message.get("reasoning_content") == "private trace"
               for message in json.loads(posts[0].content)["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("message,expected", [
    ({}, "unknown"),
    (True, "unknown"),
    (False, "unsupported"),
    ({"$ref": "#/components/schemas/Missing"}, "unknown"),
    ({"$ref": "https://example.invalid/schema.json"}, "unknown"),
    ({"$ref": "#/components/schemas/ChatMessage"}, "unknown"),
    ({"type": "string"}, "unsupported"),
    ({"type": "object", "additionalProperties": False}, "unsupported"),
    ({"properties": {"reasoning_content": False}}, "unsupported"),
    ({"properties": {"reasoning_content": {"type": "integer"}}}, "unsupported"),
    ({"properties": {"reasoning_content": {"type": ["string", "null"]}}}, "supported"),
    ({"properties": {"reasoning_content": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}, "supported"),
    ({"$ref": "#/components/schemas/Reason~1Turn"}, "supported"),
    ({"allOf": [{"properties": {"role": {"const": "assistant"}}},
                {"properties": {"reasoning_content": {"type": "string"}}}]}, "supported"),
    ({"allOf": [{"properties": {"reasoning_content": {"type": "string"}}},
                {"additionalProperties": False}]}, "unsupported"),
    ({"allOf": [{"properties": {"reasoning_content": {"type": "string"}}},
                {"$ref": "#/components/schemas/Missing"}]}, "unknown"),
    ({"anyOf": [{"properties": {"role": {"const": "user"}}, "additionalProperties": False},
                {"properties": {"role": {"enum": ["assistant"]}, "reasoning_content": {}}}]}, "supported"),
    ({"anyOf": [{"additionalProperties": False}, {"additionalProperties": True}]}, "unknown"),
    ({"anyOf": [{"additionalProperties": False}, {"properties": {"reasoning_content": False}}]}, "unsupported"),
    ({"oneOf": [{"properties": {"reasoning_content": {}}}, {}]}, "unknown"),
    ({"oneOf": [{"properties": {"role": {"const": "user"}}, "additionalProperties": False},
                {"properties": {"role": {"const": "assistant"}, "reasoning_content": {}}}]}, "supported"),
    ({"properties": {"reasoning_content": {}}, "not": {"required": ["reasoning_content"]}}, "unknown"),
    ({"additionalProperties": False, "patternProperties": {"reasoning.*": {"type": "string"}}}, "unknown"),
])
async def test_message_schema_variants_report_conservatively(message, expected):
    spec = gateway_schema(message)
    spec["components"]["schemas"]["Reason/Turn"] = {"properties": {"reasoning_content": {}}}
    async with endpoint(spec=spec) as (provider, posts):
        capabilities = await provider.inspect_capabilities()
    assert capabilities.chat_completions.replay_reasoning_content == expected
    assert posts == []


@pytest.mark.asyncio
async def test_request_composition_and_nested_refs_are_inspected():
    spec = gateway_schema({"properties": {"reasoning_content": {}}})
    schemas = spec["components"]["schemas"]
    schemas["Messages"] = schemas["ChatRequest"]["properties"]["messages"]
    schemas["ChatRequest"]["properties"]["messages"] = {"$ref": "#/components/schemas/Messages"}
    spec["paths"]["/v1/chat/completions"]["post"]["requestBody"]["content"]["application/json"]["schema"] = {
        "allOf": [{"$ref": "#/components/schemas/ChatRequest"}, {"type": "object"}],
    }
    async with endpoint(spec=spec, reasoning_effort="medium",
                        chat_reasoning_field="reasoning_content") as (provider, posts):
        capabilities = await provider.inspect_capabilities()
        await provider.complete(_request())
    assert capabilities.chat_completions.replay_reasoning_content == "supported"
    assert len(posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [
    False, {"type": "string"}, {"$ref": "#/components/schemas/Missing"},
    {"type": "boolean", "const": False}, {"type": "boolean", "enum": [False]},
])
async def test_include_switch_requires_a_supported_boolean_property(declared):
    spec = gateway_schema()
    spec["components"]["schemas"]["ChatRequest"]["properties"]["include_reasoning"] = declared
    async with endpoint(spec=spec, reasoning_effort="medium") as (provider, posts):
        await provider.complete(_simple_request())
    assert "include_reasoning" not in json.loads(posts[0].content)


@pytest.mark.asyncio
async def test_enable_thinking_and_chat_reasoning_object_do_not_establish_effort():
    spec = gateway_schema()
    properties = spec["components"]["schemas"]["ChatRequest"]["properties"]
    properties.pop("reasoning_effort")
    properties["reasoning"] = {"type": "object"}
    async with endpoint(spec=spec, reasoning_effort="medium") as (provider, posts):
        with pytest.raises(ProviderCapabilityError, match="reasoning_effort"):
            await provider.complete(_simple_request())
        assert posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("responses_replay", [False, True])
async def test_verified_responses_is_preferred_to_unverified_chat_replay(responses_replay):
    spec = _openapi()
    if not responses_replay:
        spec["paths"]["/v1/responses"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"].pop("reasoning")
    async with endpoint(spec=spec, chat_reasoning_field="reasoning_content") as (provider, posts):
        response = await provider.complete(_request())
    assert len(posts) == 1
    assert response.metadata.protocol == ("responses" if responses_replay else "chat_completions")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_prepared_count_and_dispatch_keep_replay_and_effort(stream):
    spec = gateway_schema()
    spec["paths"]["/v1/chat/completions/render"] = {"post": {}}
    async with endpoint(spec=spec, native=True, chat_reasoning_field="reasoning_content",
                        reasoning_effort="medium", token_counting="vllm") as (provider, posts):
        call = await provider.prepare_call(_request(), stream=stream)
        count = await provider.count_prepared(call)
        if stream:
            [event async for event in provider.stream_prepared(call)]
        else:
            await provider.complete_prepared(call)
    assert count.count == 32
    rendered, dispatched = [json.loads(post.content) for post in posts]
    assert dispatched == call.payload
    assert rendered == dispatched
    assert dispatched["reasoning_effort"] == "medium"
    assert "include_reasoning" not in dispatched


@pytest.mark.asyncio
async def test_response_reasoning_does_not_change_configured_input_field():
    def respond(request):
        response = completion_response("chat_completions", False).json()
        response["choices"][0]["message"]["reasoning"] = "new trace"
        return httpx.Response(200, json=response)

    async with endpoint(chat_reasoning_field="reasoning_content", responder=respond) as (provider, posts):
        first = await provider.complete(_simple_request())
        await provider.complete(ModelRequest(instructions="", items=(
            ModelAssistantTurn(source_id="a", content=first.content, reasoning=first.reasoning),
            ModelUserInput(source_id="u2", content="continue"),
        )))
    assert first.reasoning == "new trace"
    assistant = next(message for message in json.loads(posts[1].content)["messages"] if message["role"] == "assistant")
    assert assistant["reasoning_content"] == "new trace"
    assert "reasoning" not in assistant


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_gateway_tool_exchange_replays_without_reexecuting_tool(tmp_path, stream):
    from examples.reasoning_gateway import exercise

    result = await exercise(tmp_path, stream=stream)
    assert result["model_requests"] == 2
    assert result["tool_executions"] == 1
    assert result["output"] == "gateway-value-42"


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"extra_body": {"messages": [{"role": "user", "content": "replacement"}]}},
    {"extra_request_args": {"messages": [{"role": "user", "content": "replacement"}]}},
    {"extra_body": {"messages": [{"role": "assistant", "reasoning_content": "discarded"}]},
     "extra_request_args": {"extra_body": {"messages": [{"role": "user", "content": "replacement"}]}}},
    {"extra_body": {"messages": [{"role": "user", "content": "replacement"}]},
     "extra_request_args": {"messages": [{"role": "assistant", "reasoning_content": "discarded"}]}},
])
async def test_replay_validation_respects_final_message_overrides(extra):
    spec = gateway_schema({"type": "object", "additionalProperties": False,
                           "properties": {"role": {}, "content": {}, "tool_calls": {}}})
    async with endpoint(spec=spec, chat_reasoning_field="reasoning_content", **extra) as (provider, posts):
        response = await provider.complete(_request())
    assert response.content == "done"
    assert json.loads(posts[0].content)["messages"] == [{"role": "user", "content": "replacement"}]
    assert response.metadata.request_reasoning.serialized_items == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_replay_validation_checks_actual_overridden_fields(stream):
    spec = gateway_schema({"properties": {"reasoning": {}, "reasoning_content": False}})
    async with endpoint(spec=spec, chat_reasoning_field="reasoning", extra_body={"messages": [
        {"role": "assistant", "content": "previous", "reasoning_content": "forbidden"},
    ]}) as (provider, posts):
        with pytest.raises(ProviderCapabilityError, match="replay_reasoning_content"):
            await invoke(provider, _simple_request(), stream)
        assert posts == []
