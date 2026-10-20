from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from openai import BadRequestError, PermissionDeniedError

from dagent.config import ProviderConfig
from dagent.providers import OpenAICompatibleProvider, ProviderCapabilityError
from dagent.providers.openai_compatible import ProviderTokenCountError
from dagent.providers.model_io import ModelRequest, ModelUserInput
from tests.test_model_protocols import DualProtocolClient, _openapi


def request(**kwargs) -> ModelRequest:
    return ModelRequest(
        instructions="Stable instructions",
        items=(ModelUserInput(source_id="user", content="Continue"),),
        strict_context_budget=True,
        **kwargs,
    )


def provider(client, **kwargs) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1", model="local-model",
            protocol="chat_completions", **kwargs,
        ),
        client=client,
    )


class RenderClient(DualProtocolClient):
    def __init__(self, *, render_error=None):
        schema = _openapi()
        schema["paths"]["/v1/chat/completions/render"] = {"post": {}}
        schema["paths"]["/v1/responses/render"] = {"post": {}}
        super().__init__(openapi=schema)
        self.render_error = render_error

    async def post(self, path, *, body, **kwargs):
        if path.endswith("/render"):
            self.raw_posts.append((path, body))
            if self.render_error is not None:
                raise self.render_error
            return {"token_ids": [10, 20, 30], "sampling_params": {"max_tokens": 512}}
        return await super().post(path, body=body, **kwargs)


@pytest.mark.asyncio
async def test_prepared_count_and_send_share_final_extra_overlay_and_snapshot():
    client = RenderClient()
    tool = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
    configured = provider(
        client, max_output_tokens=256,
        extra_request_args={"temperature": 0.5, "extra_body": {
            "temperature": 0.1, "max_completion_tokens": 1024,
            "chat_template_kwargs": {"enable_thinking": False},
        }},
    )
    call = await configured.prepare_call(request(tools=(tool,), max_output_tokens=2048, purpose="compaction"))
    expected = call.payload
    assert call.effective_max_output_tokens == 1024
    assert expected["temperature"] == 0.1
    configured.config.extra_request_args["extra_body"]["temperature"] = 1
    configured.config.extra_request_args["extra_body"]["chat_template_kwargs"]["enable_thinking"] = True
    configured.config.model = "changed-model"
    tool["function"]["name"] = "changed-tool"
    call.kwargs["extra_body"]["temperature"] = 2
    call.payload["messages"].clear()
    count = await configured.count_prepared(call)
    assert count.count == 3 and count.exact is True and count.source == "render"
    assert client.raw_posts == [("/v1/chat/completions/render", expected)]
    await configured.complete_prepared(call)
    sent = dict(client.completions.calls[-1])
    sent.update(sent.pop("extra_body"))
    assert sent == expected


@pytest.mark.asyncio
async def test_strict_summary_output_is_independent_while_legacy_ceiling_remains():
    client = DualProtocolClient()
    configured = provider(client, max_output_tokens=256)
    strict = request(max_output_tokens=2048, purpose="compaction")
    await configured.complete(strict)
    assert client.completions.calls[-1]["max_completion_tokens"] == 2048
    await configured.complete(replace(strict, strict_context_budget=False))
    assert client.completions.calls[-1]["max_completion_tokens"] == 256


@pytest.mark.asyncio
async def test_strict_default_output_budget_is_present_on_wire():
    client = DualProtocolClient()
    configured = provider(client)
    await configured.complete(request())
    assert client.completions.calls[-1]["max_completion_tokens"] == 8192
    await configured.complete(replace(request(), strict_context_budget=False))
    assert "max_completion_tokens" not in client.completions.calls[-1]


@pytest.mark.asyncio
async def test_prepared_stream_keeps_original_protocol_and_arguments():
    client = RenderClient()
    configured = provider(client)
    call = await configured.prepare_call(request(max_output_tokens=512), stream=True)
    configured.config.protocol = "responses"
    configured.config.max_output_tokens = 1
    events = [event async for event in configured.stream_prepared(call)]
    assert events[-1].response.content == "chat stream"
    assert client.completions.calls[-1]["max_completion_tokens"] == 512
    assert not client.responses.calls
    with pytest.raises(ValueError, match="stream mode"):
        await configured.complete_prepared(call)
    with pytest.raises(ValueError, match="another provider"):
        await provider(DualProtocolClient()).count_prepared(call)


@pytest.mark.asyncio
async def test_responses_render_uses_responses_payload_without_chat_conversion():
    client = RenderClient()
    configured = provider(client)
    configured.config.protocol = "responses"
    call = await configured.prepare_call(request(max_output_tokens=512))
    count = await configured.count_prepared(call)
    assert count.exact is True
    path, body = client.raw_posts[-1]
    assert path == "/v1/responses/render"
    assert "input" in body and "messages" not in body
    await configured.complete_prepared(call)
    assert client.responses.calls[-1] == call.kwargs


@pytest.mark.asyncio
async def test_compatible_tokenize_forwards_template_options():
    schema = _openapi()
    schema["paths"]["/tokenize"]["post"] = {
        "requestBody": {"content": {"application/json": {"schema": {"properties": {
            "model": {}, "messages": {}, "chat_template_kwargs": {}, "add_generation_prompt": {},
        }}}}}
    }
    client = DualProtocolClient(openapi=schema)
    configured = provider(client, extra_body={"chat_template_kwargs": {"enable_thinking": False}, "add_generation_prompt": True})
    count = await configured.count_prepared(await configured.prepare_call(request(max_output_tokens=512)))
    assert count.source == "tokenize" and count.exact is True
    body = client.raw_posts[-1][1]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["add_generation_prompt"] is True


@pytest.mark.asyncio
async def test_tokenize_tools_are_not_claimed_exact_and_unsupported_renderer_falls_back():
    client = DualProtocolClient()
    configured = provider(client)
    tool = {"type": "function", "function": {"name": "lookup"}}
    count = await configured.count_prepared(await configured.prepare_call(request(tools=(tool,))))
    assert count.source == "tokenize" and count.exact is False
    configured.config.extra_body["chat_template_kwargs"] = {"enable_thinking": True}
    assert await configured.count_prepared(await configured.prepare_call(request())) is None
    assert len(client.raw_posts) == 1


@pytest.mark.asyncio
async def test_render_unavailable_falls_back_once_to_tokenize():
    response = httpx.Response(403, request=httpx.Request("POST", "http://localhost/render"))
    client = RenderClient(render_error=PermissionDeniedError("disabled", response=response, body={}))
    configured = provider(client)
    call = await configured.prepare_call(request())
    assert (await configured.count_prepared(call)).source == "tokenize"
    assert (await configured.count_prepared(call)).source == "tokenize"
    assert [path for path, _ in client.raw_posts].count("/v1/chat/completions/render") == 1


@pytest.mark.asyncio
async def test_render_400_is_diagnostic_and_never_generates_or_retries():
    response = httpx.Response(400, request=httpx.Request("POST", "http://localhost/render"))
    client = RenderClient(render_error=BadRequestError("context or another validation error", response=response, body={}))
    configured = provider(client)
    with pytest.raises(ProviderTokenCountError, match="rejected the prepared request"):
        await configured.count_prepared(await configured.prepare_call(request()))
    assert [path for path, _ in client.raw_posts] == ["/v1/chat/completions/render", "/tokenize"]
    assert not client.completions.calls


@pytest.mark.asyncio
async def test_render_400_can_report_independently_proven_oversized_input():
    response = httpx.Response(400, request=httpx.Request("POST", "http://localhost/render"))
    client = RenderClient(render_error=BadRequestError("validation rejected", response=response, body={}))
    original_post = client.post

    async def post(path, *, body, **kwargs):
        if path == "/tokenize":
            client.raw_posts.append((path, body))
            return {"count": 130000, "max_model_len": 131072}
        return await original_post(path, body=body, **kwargs)

    client.post = post
    configured = provider(client)
    count = await configured.count_prepared(await configured.prepare_call(request(max_output_tokens=8192)))
    assert count.count == 130000
    assert count.exact is False and count.source == "tokenize"
    assert not client.completions.calls


@pytest.mark.asyncio
async def test_explicit_heuristic_does_not_contact_count_endpoints():
    client = RenderClient()
    configured = provider(client, token_counting="heuristic")
    assert await configured.count_prepared(await configured.prepare_call(request())) is None
    assert not client.raw_posts


@pytest.mark.asyncio
async def test_strict_server_window_shrinks_and_refreezes_automatic_output_budget():
    from dagent.harness_runtime.context import ContextAssembler
    from dagent.schemas import ContextPolicy, ConversationState, UserMessage

    client = DualProtocolClient()
    original_post = client.post

    async def post(path, *, body, **kwargs):
        if path == "/tokenize":
            client.raw_posts.append((path, body))
            return {"count": 100, "max_model_len": 4096}
        return await original_post(path, body=body, **kwargs)

    client.post = post
    configured = provider(client, context_window_tokens=131072)
    assembler = ContextAssembler(
        context_window_tokens=131072,
        request_preparer=configured.prepare_call, prepared_token_counter=configured.count_prepared,
    )
    prepared = await assembler.prepare(
        system_message={"role": "system", "content": "Short instructions"},
        conversation=ConversationState(items=(UserMessage(content="Hello"),)), tools=(),
        policy=ContextPolicy(compaction_strategy="chunked"), enable_new_strategy=True,
    )
    assert configured.context_window_tokens == 4096
    assert prepared.usage.context_window_tokens == 4096
    assert prepared.usage.max_output_tokens == 512
    assert prepared.usage.input_budget_tokens == 3584
    assert prepared.prepared_call.effective_max_output_tokens == 512
    assert len(client.raw_posts) == 2
    assert not client.completions.calls
    await configured.complete_prepared(prepared.prepared_call)
    assert client.completions.calls[-1]["max_completion_tokens"] == 512


@pytest.mark.asyncio
async def test_count_uses_frozen_root_and_transport_options():
    client = RenderClient()
    roots = []
    options = []
    original_post = client.post

    def with_options(**kwargs):
        if "base_url" in kwargs:
            roots.append(kwargs["base_url"])
        return client

    async def post(path, *, body, **kwargs):
        options.append(kwargs.get("options"))
        return await original_post(path, body=body, **kwargs)

    client.with_options = with_options
    client.post = post
    configured = provider(client, extra_request_args={"extra_headers": {"X-Tenant": "original"}, "extra_query": {"deployment": "one"}})
    call = await configured.prepare_call(request())
    configured.config.base_url = "http://different.invalid/v1"
    configured.config.extra_request_args["extra_headers"]["X-Tenant"] = "changed"
    await configured.count_prepared(call)
    assert roots[-1] == "http://localhost:8000/"
    assert options[-1] == {"headers": {"X-Tenant": "original"}, "params": {"deployment": "one"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [{"max_completion_tokens": None}, {"truncate_prompt_tokens": -1}])
async def test_strict_budget_rejects_unbounded_output_or_silent_server_truncation(override):
    configured = provider(DualProtocolClient(), extra_request_args={"extra_body": override})
    with pytest.raises(ProviderCapabilityError):
        await configured.prepare_call(request())


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    {"messages": [{"role": "user", "content": "replaced history"}]},
    {"model": "different-model"},
    {"tools": [{"type": "function", "function": {"name": "different-tool"}}]},
])
async def test_strict_projection_rejects_extra_overrides_but_legacy_keeps_precedence(override):
    client = DualProtocolClient()
    configured = provider(client, extra_request_args={"extra_body": override})
    with pytest.raises(ProviderCapabilityError, match="cannot override the projected"):
        await configured.prepare_call(request())
    await configured.complete(replace(request(), strict_context_budget=False))
    assert client.completions.calls[-1]["extra_body"] == override


@pytest.mark.asyncio
async def test_strict_projection_allows_identical_history_overlay():
    configured = provider(DualProtocolClient())
    original = await configured.prepare_call(request())
    configured.config.extra_request_args["extra_body"] = {
        "model": original.payload["model"], "messages": original.payload["messages"],
    }
    assert (await configured.prepare_call(request())).payload == original.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("field, value", [
    ("input", [{"role": "user", "content": "replaced history"}]),
    ("instructions", "replaced system prompt"),
])
async def test_strict_responses_projection_rejects_extra_history(field, value):
    configured = provider(DualProtocolClient(), extra_body={field: value})
    configured.config.protocol = "responses"
    with pytest.raises(ProviderCapabilityError, match=f"projected '{field}'"):
        await configured.prepare_call(request())


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_compaction_rejects_external_fixed_schema_while_generation_accepts(protocol):
    schema = {"type": "json_schema", "json_schema": {"name": "Other", "schema": {"type": "object"}}}
    override = {"response_format": schema} if protocol == "chat_completions" else {"text": {"format": schema}}
    client = DualProtocolClient()
    configured = provider(client, extra_body=override)
    configured.config.protocol = protocol
    with pytest.raises(ProviderCapabilityError, match="task summary schema"):
        await configured.prepare_call(request(purpose="compaction"))
    call = await configured.prepare_call(request())
    assert all(call.payload[key] == value for key, value in override.items())


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_context_candidate_sends_the_same_wire_request_that_was_counted(stream):
    from dagent.harness_runtime.context import ContextAssembler
    from dagent.schemas import ContextPolicy, ConversationState, UserMessage

    client = RenderClient()
    configured = provider(client, max_output_tokens=512, extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    assembler = ContextAssembler(
        context_window_tokens=131072, max_output_tokens=512,
        request_preparer=configured.prepare_call, prepared_token_counter=configured.count_prepared,
    )
    candidate = await assembler.prepare(
        system_message={"content": "Stable instructions"},
        conversation=ConversationState(items=(UserMessage(content="Continue"),)),
        policy=ContextPolicy(compaction_strategy="chunked"), enable_new_strategy=True,
        stream=stream,
    )
    assert candidate.usage.estimated_input_tokens == 3
    assert candidate.usage.count_precision == "rendered"
    measured = client.raw_posts[-1][1]
    configured.config.extra_body["chat_template_kwargs"]["enable_thinking"] = True
    if stream:
        _ = [event async for event in configured.stream_prepared(candidate.prepared_call)]
    else:
        await configured.complete_prepared(candidate.prepared_call)
    sent = dict(client.completions.calls[-1])
    sent.update(sent.pop("extra_body"))
    assert sent == measured


@pytest.mark.asyncio
async def test_final_protected_candidate_can_fit_despite_heuristic_overestimate():
    from dagent.harness_runtime.context import ContextAssembler
    from dagent.schemas import ContextPolicy, ConversationState, UserMessage

    client = RenderClient()
    configured = provider(client, context_window_tokens=4096, max_output_tokens=512)
    assembler = ContextAssembler(
        context_window_tokens=4096, max_output_tokens=512,
        request_preparer=configured.prepare_call, prepared_token_counter=configured.count_prepared,
    )
    content = "a" * 30000
    prepared = await assembler.prepare(
        system_message={"content": "Short instructions"},
        conversation=ConversationState(items=(UserMessage(run_id="active", content=content),)),
        tools=(), active_run_id="active", policy=ContextPolicy(compaction_strategy="chunked"),
        enable_new_strategy=True,
    )
    assert prepared.usage.estimated_input_tokens == 3
    assert prepared.usage.count_precision == "rendered"
    assert prepared.prepared_call.payload["messages"][-1]["content"] == content
    assert client.raw_posts == [("/v1/chat/completions/render", prepared.prepared_call.payload)]
    assert not client.completions.calls
    await configured.complete_prepared(prepared.prepared_call)
    assert client.completions.calls[-1] == prepared.prepared_call.kwargs


@pytest.mark.asyncio
async def test_unknown_custom_counter_precision_keeps_the_safety_margin():
    from dagent.harness_runtime.context import ContextAssembler
    from dagent.providers.model_io import ModelTokenCount
    from dagent.schemas import ContextPolicy, ConversationState, UserMessage

    async def custom_count(request, field):
        return ModelTokenCount(count=1000, estimator="custom")

    candidate = await ContextAssembler(
        context_window_tokens=8192, max_output_tokens=512, request_token_counter=custom_count,
    ).prepare(
        system_message={"content": "Stable instructions"},
        conversation=ConversationState(items=(UserMessage(content="Continue"),)),
        policy=ContextPolicy(compaction_strategy="chunked"), enable_new_strategy=True,
    )
    assert candidate.usage.estimated_input_tokens == 1150
    assert candidate.usage.count_precision == "estimated"
