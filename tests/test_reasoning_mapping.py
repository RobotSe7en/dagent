"""Public configuration and wire-level reasoning mapping contracts."""

from contextlib import asynccontextmanager, closing
from dataclasses import replace
import json

import httpx
from openai import AsyncOpenAI
import pytest
import yaml

import dagent
from dagent.config import ProviderConfig, ReasoningMapping, load_config
from dagent.providers import ProviderCapabilityError, ProviderRequestError
from tests.test_model_protocols import _openapi, _request, _simple_request
from tests.test_request_reasoning import completion_response


def _schema_without_reasoning():
    spec = _openapi()
    properties = spec["components"]["schemas"]["ChatRequest"]["properties"]
    properties.pop("reasoning_effort")
    properties.pop("include_reasoning")
    spec["paths"]["/v1/responses"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"].pop("reasoning")
    return spec


@asynccontextmanager
async def _endpoint(*, spec=None, unavailable=False, responder=None, **options):
    posts = []

    def handle(request):
        if request.method == "GET":
            if unavailable:
                return httpx.Response(404)
            return httpx.Response(200, json=(
                spec if spec is not None else _openapi()
            ) if request.url.path == "/openapi.json" else {"version": "test"})
        posts.append(request)
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"count": 32, "max_model_len": 131072})
        if responder is not None:
            return responder(request)
        protocol = "responses" if request.url.path.endswith("/responses") else "chat_completions"
        return completion_response(protocol, json.loads(request.content).get("stream", False))

    async with AsyncOpenAI(
        api_key="synthetic", base_url="http://model.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as client:
        yield dagent.Provider(
            base_url="http://model.test/v1", model="test", client=client, **options,
        ), posts


async def _invoke(provider, stream, request=None, *, legacy=False):
    request = request or _simple_request()
    if legacy:
        if stream:
            events = [event async for event in provider.stream_chat([{"role": "user", "content": "hello"}])]
            return events[-1].response
        return await provider.chat([{"role": "user", "content": "hello"}])
    if stream:
        events = [event async for event in provider.stream(request)]
        return events[-1].response
    return await provider.complete(request)


def test_mapping_python_yaml_and_serialization(tmp_path):
    mapping = ReasoningMapping(field="custom.level", values={"high": "deep"})
    config = ProviderConfig(
        base_url="http://model.test/v1", model="test", reasoning_effort="high",
        reasoning_mapping={"chat_completions": mapping},
    )
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"provider": config.model_dump()}))
    restored = load_config(path).provider
    assert restored == config
    assert restored.reasoning_mapping["chat_completions"] == mapping
    assert ProviderConfig(base_url="http://model.test/v1", model="test").reasoning_mapping is None
    assert not hasattr(dagent, "ReasoningMapping")


@pytest.mark.parametrize("field", [
    "", ".effort", "reasoning.", "reasoning..effort", "reasoning[0].effort",
    "reasoning. effort", "model", "messages.level", "tools", "max_output_tokens",
    "store", "previous_response_id", "thinking_token_budget",
])
def test_invalid_mapping_paths_are_rejected(field):
    with pytest.raises(ValueError, match="field"):
        ReasoningMapping(field=field)


@pytest.mark.parametrize("value", [True, False, 42, 1.5, None, {}, []])
def test_mapping_values_are_strings(value):
    with pytest.raises(ValueError, match="values"):
        ReasoningMapping(field="effort", values={"high": value})


def test_mapping_rejects_unknown_protocols_efforts_and_options():
    for mapping in (
        {"auto": {"field": "effort"}},
        {"responses": {"field": "effort", "values": {"extreme": "deep"}}},
        {"responses": {"field": "effort", "unexpected": True}},
    ):
        with pytest.raises(ValueError, match="reasoning_mapping"):
            dagent.Provider(base_url="http://model.test/v1", model="test", reasoning_mapping=mapping)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("field", ["custom_effort", "custom.reasoning.level"])
async def test_mapping_reaches_wire_without_builtin_controls(protocol, stream, field):
    mapping = {protocol: {"field": field, "values": {"high": "deep"}}}
    async with _endpoint(
        spec=_schema_without_reasoning(), protocol=protocol,
        reasoning_effort="high", reasoning_mapping=mapping,
    ) as (provider, posts):
        capabilities = await provider.inspect_capabilities()
        assert getattr(capabilities, protocol).reasoning_effort == "unsupported"
        assert await provider.context_reasoning_field(_simple_request(), stream=stream) in {"reasoning", "omit"}
        assert (await provider.count_tokens(_simple_request())).count == 32
        response = await _invoke(provider, stream)
    tokenize, generation = [json.loads(post.content) for post in posts]
    assert set(tokenize) == {"model", "messages"}
    value = generation
    for part in field.split("."):
        value = value[part]
    assert value == "deep"
    assert not {"reasoning_effort", "reasoning", "include_reasoning"}.intersection(generation)
    assert response.metadata.requested_reasoning_effort == "high"
    assert response.metadata.effective_reasoning_effort == "deep"
    assert field in response.metadata.fallback_reason
    if protocol == "responses":
        assert generation["store"] is False
        assert "previous_response_id" not in generation


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max", None])
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_partial_value_maps_and_unset_effort(protocol, effort):
    async with _endpoint(
        protocol=protocol, reasoning_effort=effort,
        reasoning_mapping={protocol: {"field": "level", "values": {"high": "deep"}}},
    ) as (provider, posts):
        response = await provider.complete(_simple_request())
    payload = json.loads(posts[0].content)
    assert payload.get("level") == ("deep" if effort == "high" else effort)
    assert ("level" in payload) == (effort is not None)
    assert response.metadata.requested_reasoning_effort == effort
    assert response.metadata.effective_reasoning_effort == payload.get("level")


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_legacy_chat_entrypoints_use_chat_mapping(stream):
    async with _endpoint(
        spec=_schema_without_reasoning(), protocol="responses", reasoning_effort="high",
        reasoning_mapping={
            "chat_completions": {"field": "level", "values": {"high": "chat-deep"}},
            "responses": {"field": "other.level", "values": {"high": "responses-deep"}},
        },
    ) as (provider, posts):
        response = await _invoke(provider, stream, legacy=True)
    assert posts[0].url.path == "/v1/chat/completions"
    assert json.loads(posts[0].content)["level"] == "chat-deep"
    assert response.metadata.effective_reasoning_effort == "chat-deep"


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping_protocol", ["chat_completions", "responses"])
async def test_auto_can_select_unmapped_protocol_with_builtin_behavior(mapping_protocol):
    # Responses is preferred even when only Chat has a custom mapping.
    async with _endpoint(
        reasoning_effort="high",
        reasoning_mapping={mapping_protocol: {"field": "level", "values": {"high": "deep"}}},
    ) as (provider, posts):
        response = await provider.complete(_simple_request())
    payload = json.loads(posts[0].content)
    assert response.metadata.protocol == "responses"
    if mapping_protocol == "chat_completions":
        assert payload["reasoning"] == {"effort": "high"}
        assert payload["include_reasoning"] is True
        assert "level" not in payload
    else:
        assert payload["level"] == "deep"
        assert "include_reasoning" not in payload


@pytest.mark.asyncio
async def test_auto_selects_mapped_chat_when_builtin_responses_cannot_satisfy_effort():
    async with _endpoint(
        spec=_schema_without_reasoning(), reasoning_effort="high",
        reasoning_mapping={"chat_completions": {"field": "level"}},
    ) as (provider, posts):
        response = await provider.complete(_simple_request())
    assert response.metadata.protocol == "chat_completions"
    assert json.loads(posts[0].content)["level"] == "high"


@pytest.mark.asyncio
async def test_unmapped_chat_keeps_native_controls():
    async with _endpoint(
        protocol="chat_completions", reasoning_effort="high",
        reasoning_mapping={"responses": {"field": "level"}},
    ) as (provider, posts):
        response = await provider.complete(_simple_request())
    payload = json.loads(posts[0].content)
    assert payload["reasoning_effort"] == "high"
    assert payload["include_reasoning"] is True
    assert response.metadata.effective_reasoning_effort == "high"
    assert "mapping" not in response.metadata.fallback_reason


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("mapping", [None, {}])
async def test_absent_mapping_preserves_wire_payload_and_metadata(protocol, mapping):
    async with _endpoint(protocol=protocol, reasoning_effort="high", reasoning_mapping=mapping) as (provider, posts):
        response = await provider.complete(_simple_request())
    expected = {"model": "test", "include_reasoning": True}
    if protocol == "chat_completions":
        expected.update({
            "messages": [{"role": "system", "content": "Be useful."}, {"role": "user", "content": "hello"}],
            "reasoning_effort": "high",
        })
    else:
        expected.update({
            "instructions": "Be useful.", "input": [{"role": "user", "content": "hello"}],
            "reasoning": {"effort": "high"}, "store": False,
        })
    assert json.loads(posts[0].content) == expected
    assert response.metadata.effective_reasoning_effort == "high"
    assert "mapping" not in response.metadata.fallback_reason


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("protocol,field", [
    ("chat_completions", "reasoning_effort"), ("responses", "reasoning.effort"),
])
@pytest.mark.parametrize("source", ["extra_request_args", "extra_body", "nested_extra_body"])
async def test_explicit_options_override_mapped_defaults(protocol, field, stream, source):
    root = "reasoning_effort" if protocol == "chat_completions" else "reasoning"
    override = {root: "override" if protocol == "chat_completions" else {"effort": "override", "summary": "auto"}}
    options = {source: override} if source != "nested_extra_body" else {"extra_request_args": {"extra_body": override}}
    async with _endpoint(
        protocol=protocol, reasoning_effort="high",
        reasoning_mapping={protocol: {"field": field, "values": {"high": "deep"}}},
        **options,
    ) as (provider, posts):
        response = await _invoke(provider, stream)
    payload = json.loads(posts[0].content)
    assert payload[root] == override[root]
    assert response.metadata.requested_reasoning_effort == "high"
    assert response.metadata.effective_reasoning_effort == "override"


@pytest.mark.asyncio
async def test_explicit_extra_body_replaces_generated_body_as_before():
    async with _endpoint(
        protocol="responses", reasoning_effort="high",
        reasoning_mapping={"responses": {"field": "reasoning.effort"}},
        extra_body={"temperature": 0},
        extra_request_args={"extra_body": {"custom": True}},
    ) as (provider, posts):
        response = await provider.complete(_simple_request())
    payload = json.loads(posts[0].content)
    assert payload["custom"] is True
    assert not {"reasoning", "temperature"}.intersection(payload)
    assert response.metadata.effective_reasoning_effort is None


@pytest.mark.asyncio
async def test_nested_body_overrides_merge_and_metadata_reads_final_wire():
    async with _endpoint(
        protocol="responses", reasoning_effort="high",
        reasoning_mapping={"responses": {"field": "custom.level"}},
        extra_body={"custom": {"other": True, "level": "override"}},
    ) as (provider, posts):
        response = await provider.complete(_simple_request())
    assert json.loads(posts[0].content)["custom"] == {"other": True, "level": "override"}
    assert response.metadata.effective_reasoning_effort == "override"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("missing", ["tools", "streaming", "structured_output", "reasoning", "endpoint", "output_limit"])
async def test_mapping_does_not_bypass_other_capabilities(protocol, missing):
    spec = _schema_without_reasoning()
    path = "/v1/responses" if protocol == "responses" else "/v1/chat/completions"
    properties = (spec["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"]
                  if protocol == "responses" else spec["components"]["schemas"]["ChatRequest"]["properties"])
    field = {
        "tools": "tools", "streaming": "stream",
        "structured_output": "text" if protocol == "responses" else "response_format",
        "output_limit": "max_output_tokens" if protocol == "responses" else "max_completion_tokens",
    }.get(missing)
    if field:
        properties.pop(field)
    if missing == "endpoint":
        spec["paths"].pop(path)
    if missing == "reasoning" and protocol == "chat_completions":
        # Replay is constrained by the message schema, independently of effort
        # and the optional response switch removed by _schema_without_reasoning.
        properties["messages"] = {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"role": {}, "content": {}, "tool_calls": {}},
        }}
    request = _request() if missing in {"tools", "reasoning"} else _simple_request()
    if missing == "structured_output":
        from dagent.providers import StructuredOutputFormat
        request = replace(request, response_format=StructuredOutputFormat(name="answer", schema={"type": "object"}))
    if missing == "output_limit":
        request = replace(request, max_output_tokens=128)
    async with _endpoint(
        spec=spec, protocol=protocol, reasoning_effort="high", chat_reasoning_field="reasoning",
        reasoning_mapping={protocol: {"field": "level"}},
    ) as (provider, posts):
        with pytest.raises(ProviderCapabilityError):
            await _invoke(provider, missing == "streaming", request)
        assert posts == []


@pytest.mark.asyncio
async def test_probe_failure_keeps_auto_chat_and_uses_mapping():
    async with _endpoint(
        unavailable=True, reasoning_effort="high",
        reasoning_mapping={"chat_completions": {"field": "level", "values": {"high": "deep"}}},
    ) as (provider, posts):
        with pytest.warns(RuntimeWarning):
            response = await provider.complete(_simple_request())
    assert response.metadata.protocol == "chat_completions"
    assert json.loads(posts[0].content)["level"] == "deep"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 422])
async def test_mapping_rejection_is_not_downgraded_or_replayed(status):
    async with _endpoint(
        protocol="responses", reasoning_effort="high",
        reasoning_mapping={"responses": {"field": "custom.level", "values": {"high": "deep"}}},
        responder=lambda _: httpx.Response(status, json={"error": {"message": "unsupported effort", "param": "custom.level"}}),
    ) as (provider, posts):
        with pytest.raises(ProviderRequestError) as captured:
            await provider.complete(_simple_request())
    assert len(posts) == 1
    assert captured.value.metadata.effective_reasoning_effort == "deep"
    assert captured.value.metadata.requested_reasoning_effort == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("map_low", [False, True])
async def test_runner_compaction_uses_shared_mapping_and_independent_low(tmp_path, protocol, map_low):
    values = {"high": "deep", **({"low": "light"} if map_low else {})}
    async with _endpoint(
        spec=_schema_without_reasoning(), protocol=protocol, token_counting="heuristic",
        context_window_tokens=2048, max_output_tokens=256, reasoning_effort="high",
        reasoning_mapping={protocol: {"field": "custom.level", "values": values}},
    ) as (provider, posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, runtime_directory=".runtime")) as runner:
            agent = dagent.ToolAgent(profile="conversation", capabilities=[], context=dagent.ContextPolicy(
                compaction_trigger_ratio=0.2, summary_max_tokens=64,
            ))
            first = await runner.run(agent, input="x" * 600)
            second = await runner.run(agent, input="y" * 600, conversation=first.conversation)
    payloads = [json.loads(post.content) for post in posts]
    assert [body["custom"]["level"] for body in payloads] == ["deep", "light" if map_low else "low", "deep"]
    assert second.status == "completed"
    assert second.conversation.summary is not None
    assert second.conversation.items[-1].model_call.effective_reasoning_effort == "deep"
    assert dagent.ContextPolicy().compaction_reasoning_effort == "low"
