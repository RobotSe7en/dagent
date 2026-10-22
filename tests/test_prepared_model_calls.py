from __future__ import annotations

from dataclasses import replace
import json

import httpx
import pytest
from openai import BadRequestError, InternalServerError, PermissionDeniedError

from dagent.config import ProviderConfig
from dagent.providers import OpenAICompatibleProvider, ProviderCapabilityError, ProviderReprepareRequired
from dagent.providers.openai_compatible import ProviderContextWindowError, ProviderTokenCountError
from dagent.providers.model_io import ModelRequest, ModelTokenCount, ModelUserInput
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


class SmallWindowRenderClient(RenderClient):
    def __init__(self, *, max_model_len=4096, always_reject=False):
        super().__init__()
        self.max_model_len = max_model_len
        self.always_reject = always_reject

    async def post(self, path, *, body, **kwargs):
        self.raw_posts.append((path, body))
        if path == "/tokenize":
            # The Responses window probe is not a count of Responses input.
            count = 100000 if "prompt" in body else 3
            return {"count": count, "max_model_len": self.max_model_len}
        assert path.endswith("/render")
        output = body.get("max_completion_tokens", body.get("max_output_tokens"))
        if self.always_reject or output >= self.max_model_len:
            response = httpx.Response(400, request=httpx.Request("POST", "http://localhost" + path))
            raise BadRequestError("validation rejected", response=response, body={})
        return {"token_ids": [10, 20, 30]}


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
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("with_tools", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_render_rejection_discovers_small_window_before_refreezing_automatic_output(protocol, with_tools, stream):
    from dagent.harness_runtime.context import ContextAssembler
    from dagent.schemas import ContextPolicy, ConversationState, UserMessage

    client = SmallWindowRenderClient()
    configured = provider(client)
    configured.config.protocol = protocol
    tools = ({"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}},) if with_tools else ()
    assembler = ContextAssembler(
        context_window_tokens=131072,
        request_preparer=configured.prepare_call, prepared_token_counter=configured.count_prepared,
    )
    prepared = await assembler.prepare(
        system_message={"content": "Short instructions"},
        conversation=ConversationState(items=(UserMessage(content="Hello"),)), tools=tools,
        policy=ContextPolicy(compaction_strategy="chunked"), enable_new_strategy=True, stream=stream,
    )
    assert prepared.usage.context_window_tokens == 4096
    assert prepared.usage.max_output_tokens == 512
    assert prepared.usage.input_budget_tokens == 3584
    assert prepared.usage.estimated_input_tokens == 3
    assert prepared.usage.count_precision == "rendered"
    render_path = "/v1/responses/render" if protocol == "responses" else "/v1/chat/completions/render"
    assert [path for path, _ in client.raw_posts] == [render_path, "/tokenize", render_path]
    measured = client.raw_posts[-1][1]
    assert not client.completions.calls and not client.responses.calls
    if stream:
        _ = [event async for event in configured.stream_prepared(prepared.prepared_call)]
    else:
        await configured.complete_prepared(prepared.prepared_call)
    endpoint = client.responses if protocol == "responses" else client.completions
    sent = dict(endpoint.calls[-1])
    sent.update(sent.pop("extra_body", {}))
    assert sent == measured


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("with_tools", [False, True])
async def test_small_window_discovery_preserves_explicit_output_limit(protocol, with_tools):
    from dagent.harness_runtime.context import ContextAssembler
    from dagent.schemas import ContextPolicy, ConversationState, UserMessage

    client = SmallWindowRenderClient()
    configured = provider(client, max_output_tokens=8192)
    configured.config.protocol = protocol
    assembler = ContextAssembler(
        context_window_tokens=131072, max_output_tokens=8192,
        request_preparer=configured.prepare_call, prepared_token_counter=configured.count_prepared,
    )
    tools = ({"type": "function", "function": {"name": "lookup"}},) if with_tools else ()
    with pytest.raises(ProviderCapabilityError, match="output limit.*8192.*4096") as failure:
        await assembler.prepare(
            system_message={"content": "Short instructions"},
            conversation=ConversationState(items=(UserMessage(content="Hello"),)), tools=tools,
            policy=ContextPolicy(compaction_strategy="chunked"), enable_new_strategy=True,
        )
    assert failure.value._conversation.items[-1].content == "Hello"
    assert configured.max_output_tokens == 8192
    assert len(client.raw_posts) == 2
    assert not client.completions.calls and not client.responses.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("max_model_len", [4096, 131072])
async def test_window_discovery_does_not_hide_unrelated_render_400(protocol, max_model_len):
    from dagent.harness_runtime.context import ContextAssembler
    from dagent.schemas import ContextPolicy, ConversationState, UserMessage

    client = SmallWindowRenderClient(max_model_len=max_model_len, always_reject=True)
    configured = provider(client)
    configured.config.protocol = protocol
    assembler = ContextAssembler(
        context_window_tokens=131072,
        request_preparer=configured.prepare_call, prepared_token_counter=configured.count_prepared,
    )
    with pytest.raises(ProviderTokenCountError, match="validation rejected"):
        await assembler.prepare(
            system_message={"content": "Short instructions"},
            conversation=ConversationState(items=(UserMessage(content="Hello"),)), tools=(),
            policy=ContextPolicy(compaction_strategy="chunked"), enable_new_strategy=True,
        )
    expected_renders = 2 if max_model_len == 4096 else 1
    assert sum(path.endswith("/render") for path, _ in client.raw_posts) == expected_renders
    if protocol == "responses":
        assert sum(path == "/tokenize" for path, _ in client.raw_posts) == 1
    assert not client.completions.calls and not client.responses.calls


@pytest.mark.asyncio
async def test_responses_window_probe_uses_frozen_model_and_transport_once():
    client = SmallWindowRenderClient()
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
    configured = provider(client, extra_request_args={
        "extra_headers": {"X-Tenant": "original"}, "extra_query": {"deployment": "one"},
    })
    configured.config.protocol = "responses"
    call = await configured.prepare_call(request())
    configured.config.base_url = "http://different.invalid/v1"
    configured.config.model = "changed-model"
    configured.config.extra_request_args["extra_headers"]["X-Tenant"] = "changed"
    for _ in range(2):
        with pytest.raises(ProviderContextWindowError) as failure:
            await configured.count_prepared(call)
        assert failure.value.max_model_len == 4096
    probes = [body for path, body in client.raw_posts if path == "/tokenize"]
    assert probes == [{"model": "local-model", "prompt": "Context window probe"}]
    assert set(roots) == {"http://localhost:8000/"}
    assert all(value == {"headers": {"X-Tenant": "original"}, "params": {"deployment": "one"}}
               for value in options)


@pytest.mark.asyncio
@pytest.mark.parametrize("token_counting", ["auto", "vllm"])
async def test_failed_window_probe_can_recover_on_next_explicit_count(token_counting):
    client = SmallWindowRenderClient()
    original_post = client.post
    attempts = 0

    async def post(path, *, body, **kwargs):
        nonlocal attempts
        if path == "/tokenize":
            attempts += 1
            if attempts == 1:
                client.raw_posts.append((path, body))
                response = httpx.Response(500, request=httpx.Request("POST", "http://localhost/tokenize"))
                raise InternalServerError("temporarily unavailable", response=response, body={})
        return await original_post(path, body=body, **kwargs)

    client.post = post
    configured = provider(client, token_counting=token_counting)
    configured.config.protocol = "responses"
    call = await configured.prepare_call(request())
    with pytest.raises(ProviderTokenCountError, match="validation rejected"):
        await configured.count_prepared(call)
    assert attempts == 1
    with pytest.raises(ProviderContextWindowError) as failure:
        await configured.count_prepared(call)
    assert failure.value.max_model_len == 4096
    assert attempts == 2
    assert not client.completions.calls and not client.responses.calls


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
@pytest.mark.parametrize("override", [
    {"max_completion_tokens": None}, {"truncate_prompt_tokens": -1},
    {"truncation": None}, {"truncation": "auto"},
])
async def test_strict_budget_rejects_unbounded_output_or_silent_server_truncation(override):
    configured = provider(DualProtocolClient(), extra_request_args={"extra_body": override})
    with pytest.raises(ProviderCapabilityError):
        await configured.prepare_call(request())


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("truncation", [None, "auto", "disabled"])
async def test_strict_truncation_check_preserves_legacy_extra_precedence(protocol, truncation):
    client = DualProtocolClient()
    configured = provider(client, extra_body={"truncation": truncation})
    configured.config.protocol = protocol
    if truncation == "disabled":
        assert (await configured.prepare_call(request())).payload["truncation"] == "disabled"
    else:
        with pytest.raises(ProviderCapabilityError, match="truncation to be disabled"):
            await configured.prepare_call(request())
    await configured.complete(replace(request(), strict_context_budget=False))
    endpoint = client.responses if protocol == "responses" else client.completions
    assert endpoint.calls[-1]["extra_body"]["truncation"] == truncation


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


class CountingRepreparingProvider:
    """Host-owned preparation policy; SDK must count and gate each replacement."""

    def __init__(self, native, *, outputs=(512, 1024), reject_counts=(1,), reject_generation=False):
        self.native = native
        self.outputs = outputs
        self.reject_counts = reject_counts
        self.reject_generation = reject_generation
        self.prepared = []
        self.count_attempts = []
        self.counted = []
        self.sent = []
        self.rejections = []
        self.count_result = None
        self.count_error = None

    async def prepare_call(self, request, *, stream=False):
        output = self.outputs[min(len(self.prepared), len(self.outputs) - 1)]
        call = await self.native.prepare_call(replace(request, max_output_tokens=output), stream=stream)
        self.prepared.append(call)
        return call

    def reject(self, call):
        error = ProviderReprepareRequired(ValueError("explicit parameter rejection"), metadata=call.metadata)
        self.rejections.append(error)
        raise error

    async def count_prepared(self, call):
        self.count_attempts.append(call)
        if len(self.count_attempts) in self.reject_counts:
            self.reject(call)
        if self.count_error is not None:
            raise self.count_error
        count = self.count_result or await self.native.count_prepared(call)
        self.counted.append(call)
        return count

    async def complete_prepared(self, call):
        assert any(call is counted for counted in self.counted)
        self.sent.append(call)
        if self.reject_generation and len(self.sent) == 1:
            self.reject(call)
        return await self.native.complete_prepared(call)

    async def stream_prepared(self, call):
        assert any(call is counted for counted in self.counted)
        self.sent.append(call)
        if self.reject_generation and len(self.sent) == 1:
            self.reject(call)
        async for event in self.native.stream_prepared(call):
            yield event


def counting_assembler(wrapper):
    from dagent.harness_runtime.context import ContextAssembler

    return ContextAssembler(
        context_window_tokens=4096, max_output_tokens=512,
        request_preparer=wrapper.prepare_call, prepared_token_counter=wrapper.count_prepared,
    )


async def counting_candidate(assembler, *, stream=False, content="Continue"):
    from dagent.schemas import ContextPolicy, ConversationState, UserMessage

    return await assembler.prepare(
        system_message={"content": "Stable instructions"},
        conversation=ConversationState(items=(UserMessage(run_id="active", content=content),)),
        active_run_id="active", policy=ContextPolicy(compaction_strategy="cache_aware"),
        enable_new_strategy=True, stream=stream,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("force_count", [False, True])
async def test_count_reprepare_recounts_and_sends_the_same_checked_call(protocol, stream, force_count):
    from dagent.harness_runtime.model_observation import provider_events

    client = RenderClient()
    native = provider(client)
    native.config.protocol = protocol
    wrapper = CountingRepreparingProvider(native)
    candidate = await counting_candidate(
        counting_assembler(wrapper), stream=stream, content="a" * 30000 if force_count else "Continue")
    call = candidate.prepared_call
    assert len(wrapper.prepared) == len(wrapper.count_attempts) == 2
    assert wrapper.counted == [call]
    assert candidate.usage.max_output_tokens == 1024
    assert candidate.usage.input_budget_tokens == 3072
    assert candidate.usage.request_fingerprint == call.fingerprint
    assert candidate.usage.estimated_input_tokens == 3
    assert client.raw_posts[-1][1] == call.payload
    events = [event async for event in provider_events(
        wrapper, candidate.request, stream=stream, prepared=call)]
    assert events[-1].response.status == "completed"
    assert wrapper.sent[0] is wrapper.counted[-1] is call
    endpoint = client.responses if protocol == "responses" else client.completions
    sent = dict(endpoint.calls[-1])
    sent.update(sent.pop("extra_body", {}))
    assert sent == client.raw_posts[-1][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("overflow", [False, True])
async def test_generation_reprepare_can_request_another_reprepare_during_counting(stream, overflow):
    from dagent.harness_runtime.model_observation import provider_events
    from dagent.schemas import ContextWindowExceeded

    client = RenderClient()
    wrapper = CountingRepreparingProvider(provider(client), outputs=(512, 768, 3800 if overflow else 1024),
                                         reject_counts=(2,), reject_generation=True)
    if overflow:
        wrapper.count_result = ModelTokenCount(count=400, exact=True)
    assembler = counting_assembler(wrapper)
    candidate = await counting_candidate(assembler, stream=stream)

    async def reprepare():
        return (await counting_candidate(assembler, stream=stream)).prepared_call

    async def send():
        return [event async for event in provider_events(
            wrapper, candidate.request, stream=stream, prepared=candidate.prepared_call, reprepare=reprepare)]

    if overflow:
        with pytest.raises(ContextWindowExceeded):
            await send()
        assert len(wrapper.count_attempts) == 3
        assert len(wrapper.sent) == 1
        assert not client.completions.calls
        return
    events = await send()
    assert events[-1].response.status == "completed"
    assert len(wrapper.prepared) == len(wrapper.count_attempts) == 3
    assert [call.effective_max_output_tokens for call in wrapper.sent] == [512, 1024]
    assert wrapper.sent[-1] is wrapper.counted[-1]
    assert len(client.completions.calls) == 1
    assert client.raw_posts[-1][1] == wrapper.sent[-1].payload


@pytest.mark.asyncio
@pytest.mark.parametrize("output, count, window, error_type", [
    (3800, 400, 4096, "context"),
    (4096, 3, 4096, "capability"),
    (1024, 1500, 2048, "context"),
    (1024, 3, 1024, "capability"),
])
async def test_count_replacement_cannot_bypass_effective_output_or_discovered_window(output, count, window, error_type):
    from dagent.schemas import ContextWindowExceeded

    client = RenderClient()
    wrapper = CountingRepreparingProvider(provider(client), outputs=(512, output))
    wrapper.count_result = ModelTokenCount(count=count, max_model_len=window, exact=True)
    expected = ContextWindowExceeded if error_type == "context" else ProviderCapabilityError
    with pytest.raises(expected):
        await counting_candidate(counting_assembler(wrapper))
    assert len(wrapper.count_attempts) == 2
    assert not wrapper.sent and not client.completions.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("outputs, attempts, preparations", [
    ((512, 512), 1, 2),
    ((512, 1024, 512), 2, 3),
    ((512, 513, 514, 515, 516, 517), 5, 5),
])
@pytest.mark.parametrize("purpose", ["generation", "compaction"])
async def test_count_reprepare_requires_progress_and_is_bounded(outputs, attempts, preparations, purpose):
    client = RenderClient()
    wrapper = CountingRepreparingProvider(provider(client), outputs=outputs, reject_counts=range(1, 10))
    with pytest.raises(ProviderReprepareRequired) as failure:
        if purpose == "generation":
            await counting_candidate(counting_assembler(wrapper))
        else:
            await counting_compactor(wrapper)._fits(request(max_output_tokens=512, purpose="compaction"))
    assert failure.value is wrapper.rejections[-1]
    assert len(wrapper.count_attempts) == attempts
    assert len(wrapper.prepared) == preparations
    assert not wrapper.sent and not client.completions.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ProviderTokenCountError("render rejected"), ValueError("invalid"), OSError("offline")])
@pytest.mark.parametrize("purpose", ["generation", "compaction"])
async def test_ordinary_count_errors_do_not_request_repreparation(error, purpose):
    client = RenderClient()
    wrapper = CountingRepreparingProvider(provider(client), reject_counts=())
    wrapper.count_error = error
    with pytest.raises(type(error)) as failure:
        if purpose == "generation":
            await counting_candidate(counting_assembler(wrapper))
        else:
            await counting_compactor(wrapper)._fits(request(max_output_tokens=512, purpose="compaction"))
    assert failure.value is error
    assert len(wrapper.prepared) == len(wrapper.count_attempts) == 1
    assert not wrapper.rejections and not wrapper.sent and not client.completions.calls


def counting_compactor(wrapper):
    from dagent.harness_runtime.compaction import HistoryCompactor
    from dagent.schemas import ContextPolicy

    async def chat(request):
        raise AssertionError("This test dispatches the checked call directly.")

    return HistoryCompactor(
        policy=ContextPolicy(compaction_strategy="cache_aware"),
        assembler=counting_assembler(wrapper), result_store=None,
        system_message={"content": "Stable instructions"}, chat=chat,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_generation", [False, True])
async def test_compaction_reprepares_during_initial_or_replacement_count(reject_generation):
    from dagent.harness_runtime.model_observation import provider_events

    client = RenderClient()
    wrapper = CountingRepreparingProvider(
        provider(client), outputs=(512, 768, 1024) if reject_generation else (512, 1024),
        reject_counts=(2,) if reject_generation else (1,), reject_generation=reject_generation)
    engine = counting_compactor(wrapper)
    original = request(max_output_tokens=512, purpose="compaction")
    assert await engine._fits(original)
    events = [event async for event in provider_events(
        wrapper, original, stream=True, prepared=engine.prepared_call,
        reprepare=engine.reprepare, before_attempt=engine.before_attempt)]
    assert events[-1].response.status == "completed"
    assert len(wrapper.prepared) == len(wrapper.count_attempts) == (3 if reject_generation else 2)
    assert engine.prepared_request is original
    assert engine.prepared_call is wrapper.counted[-1] is wrapper.sent[-1]
    assert client.raw_posts[-1][1] == wrapper.sent[-1].payload
    assert engine.http_attempts == (2 if reject_generation else 1)
    assert engine.remaining_http_attempts == 12 - engine.http_attempts
    # The final gate must reuse the measured replacement, including its output.
    attempts = len(wrapper.count_attempts)
    assert await engine._fits(original)
    assert len(wrapper.count_attempts) == attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_generation", [False, True])
@pytest.mark.parametrize("budget", ["input", "output", "window", "heuristic"])
async def test_compaction_count_replacement_cannot_bypass_budget(reject_generation, budget):
    from dagent.harness_runtime.compaction import CompactionBudgetExceeded
    from dagent.harness_runtime.model_observation import provider_events

    client = RenderClient()
    native = provider(client, token_counting="heuristic" if budget == "heuristic" else "auto")
    output = 1024 if budget == "window" else 4096 if budget == "output" else 4095 if budget == "heuristic" else 3800
    wrapper = CountingRepreparingProvider(
        native, outputs=(512, 768, output) if reject_generation else (512, output),
        reject_counts=(2,) if reject_generation else (1,), reject_generation=reject_generation)
    if budget != "heuristic":
        wrapper.count_result = ModelTokenCount(count=400, max_model_len=1400 if budget == "window" else 4096, exact=True)
    engine = counting_compactor(wrapper)
    original = request(max_output_tokens=512, purpose="compaction")
    if reject_generation:
        assert await engine._fits(original)
        with pytest.raises(CompactionBudgetExceeded):
            async for _event in provider_events(
                wrapper, original, stream=True, prepared=engine.prepared_call,
                reprepare=engine.reprepare, before_attempt=engine.before_attempt):
                pass
        assert len(wrapper.sent) == 1
    else:
        assert not await engine._fits(original)
        assert not wrapper.sent
    assert len(wrapper.count_attempts) == (3 if reject_generation else 2)
    assert not client.completions.calls


@pytest.mark.asyncio
async def test_render_400_with_tools_reports_an_oversized_tokenizer_estimate():
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
    tool = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
    count = await configured.count_prepared(await configured.prepare_call(request(tools=(tool,), max_output_tokens=8192)))
    # Tool rendering keeps the tokenizer count approximate, but it still proves
    # the rejected request is over budget; it is never reported as exact.
    assert count.count == 130000
    assert count.exact is False and count.source == "tokenize"
    assert not client.completions.calls


class _CjkRenderClient(RenderClient):
    """Render counts like a CJK-efficient tokenizer and rejects over-window input like vLLM."""

    def __init__(self, *, max_model_len=32768, tokenize=True):
        super().__init__()
        self.max_model_len = max_model_len
        self.tokenize = tokenize

    def tokens(self, body):
        import json
        import math

        text = json.dumps(body.get("messages", []), ensure_ascii=False)
        cjk = sum(ord(character) > 0x2E80 for character in text)
        return math.ceil(cjk / 1.5) + math.ceil((len(text) - cjk) / 4)

    async def post(self, path, *, body, **kwargs):
        self.raw_posts.append((path, body))
        if path == "/tokenize":
            if not self.tokenize:
                raise PermissionDeniedError("disabled", response=httpx.Response(
                    404, request=httpx.Request("POST", "http://localhost/tokenize")), body={})
            return {"count": self.tokens(body), "max_model_len": self.max_model_len}
        output = body.get("max_completion_tokens", body.get("max_tokens", 0))
        if self.tokens(body) + output > self.max_model_len:
            response = httpx.Response(400, request=httpx.Request("POST", "http://localhost" + path))
            raise BadRequestError("prompt is too long", response=response, body={})
        return {"token_ids": [1] * self.tokens(body), "max_model_len": self.max_model_len}


class _RecordingCompactor:
    def __init__(self):
        self.calls = []
        self.source_request = None

    async def compact(self, previous, items, max_tokens, *, allow_fallback=False):
        from dagent.schemas import ContextSummary

        self.calls.append(len(items))
        return ContextSummary(content="Earlier analysis completed.", source_item_count=len(items))


def _cjk_history(paragraphs):
    from dagent.schemas import AssistantMessage, ConversationState, UserMessage

    return ConversationState(items=(
        UserMessage(id="task", run_id="run", content="请整理这些资料。"),
        *(AssistantMessage(id=f"step_{index}", run_id="run", content=f"第{index}段分析：" + "中文内容" * 135)
          for index in range(paragraphs)),
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("paragraphs", [45, 50, 55])
async def test_exact_render_count_decides_before_heuristic_compaction(tmp_path, paragraphs):
    from dagent.harness_runtime.context import ContextAssembler
    from dagent.harness_runtime.result_storage import ResultStore
    from dagent.schemas import ContextPolicy

    client = _CjkRenderClient()
    configured = provider(client, context_window_tokens=32768, max_output_tokens=4096)
    assembler = ContextAssembler(context_window_tokens=32768, max_output_tokens=4096,
                                 request_preparer=configured.prepare_call,
                                 prepared_token_counter=configured.count_prepared)
    compactor = _RecordingCompactor()
    prepared = await assembler.prepare(
        system_message={"content": "系统指令"}, conversation=_cjk_history(paragraphs),
        policy=ContextPolicy(compaction_strategy="chunked"), result_store=ResultStore(tmp_path, ".runtime"),
        enable_new_strategy=True, history_compactor=compactor,
    )
    # The local estimate exceeds the hard budget, but the rendered request is
    # 59-72% of it: below the soft trigger, so no history is summarized.
    assert prepared.usage.count_precision == "rendered"
    assert prepared.usage.estimated_input_tokens < prepared.usage.compaction_trigger_tokens
    assert compactor.calls == []
    assert len(prepared.conversation.items) == paragraphs + 1


@pytest.mark.asyncio
async def test_render_rejection_of_an_estimated_overflow_compacts_instead_of_failing(tmp_path):
    from dagent.harness_runtime.context import ContextAssembler
    from dagent.harness_runtime.result_storage import ResultStore
    from dagent.schemas import ContextPolicy

    client = _CjkRenderClient(tokenize=False)
    configured = provider(client, context_window_tokens=32768, max_output_tokens=4096)
    assembler = ContextAssembler(context_window_tokens=32768, max_output_tokens=4096,
                                 request_preparer=configured.prepare_call,
                                 prepared_token_counter=configured.count_prepared)
    compactor = _RecordingCompactor()
    prepared = await assembler.prepare(
        system_message={"content": "系统指令"}, conversation=_cjk_history(90),
        policy=ContextPolicy(compaction_strategy="chunked"), result_store=ResultStore(tmp_path, ".runtime"),
        enable_new_strategy=True, history_compactor=compactor,
    )
    assert compactor.calls
    assert prepared.usage.count_precision == "rendered"
    assert prepared.usage.estimated_input_tokens <= prepared.usage.input_budget_tokens
    renders = [path for path, _ in client.raw_posts if path.endswith("/render")]
    assert len(renders) >= 2  # The rejected original and the counted replacement.
    assert not client.completions.calls


@pytest.mark.asyncio
async def test_cache_aware_prefix_is_counted_before_falling_back_to_chunks(tmp_path):
    from dagent.harness_runtime.compaction import CompactionRecordV1, HistoryCompactor
    from dagent.harness_runtime.context import ContextAssembler
    from dagent.harness_runtime.result_storage import ResultStore
    from dagent.schemas import ContextPolicy
    from tests.test_compaction_strategy import summary_response

    client = _CjkRenderClient()
    configured = provider(client, context_window_tokens=32768, max_output_tokens=4096)
    assembler = ContextAssembler(context_window_tokens=32768, max_output_tokens=4096,
                                 request_preparer=configured.prepare_call,
                                 prepared_token_counter=configured.count_prepared)
    policy = ContextPolicy(compaction_strategy="cache_aware")
    conversation = _cjk_history(45)
    source_request, _ = assembler._project(
        system_message={"content": "系统指令"}, conversation=conversation, tools=(), policy=policy,
        active_run_id="run", omitted_reasoning_ids=set(), response_format=None, max_output_tokens=4096,
        reasoning_effort=None, purpose="generation", strict_context_budget=True,
    )

    async def chat(request):
        return summary_response(request)

    store = ResultStore(tmp_path, ".runtime")
    engine = HistoryCompactor(policy=policy, assembler=assembler, result_store=store,
                              system_message={"content": "系统指令"}, chat=chat, source_request=source_request)
    summary = await engine.compact(None, conversation.items[1:-1], 4096)
    record = CompactionRecordV1.model_validate_json(store.read_reference(summary.compaction_record))
    # The local estimate rejects this warm-prefix request; the rendered count fits.
    assert [call.kind for call in record.calls] == ["prefix"]


@pytest.mark.asyncio
async def test_render_400_that_the_tokenizer_fits_is_not_an_overflow_signal(tmp_path):
    from dagent.harness_runtime.context import ContextAssembler
    from dagent.harness_runtime.result_storage import ResultStore
    from dagent.providers.openai_compatible import ProviderRenderRejected
    from dagent.schemas import ContextPolicy

    class RejectingClient(_CjkRenderClient):
        async def post(self, path, *, body, **kwargs):
            if path.endswith("/render"):
                self.raw_posts.append((path, body))
                response = httpx.Response(400, request=httpx.Request("POST", "http://localhost" + path))
                raise BadRequestError("invalid tool schema", response=response, body={})
            return await super().post(path, body=body, **kwargs)

    client = RejectingClient()
    configured = provider(client, context_window_tokens=32768, max_output_tokens=4096)
    assembler = ContextAssembler(context_window_tokens=32768, max_output_tokens=4096,
                                 request_preparer=configured.prepare_call,
                                 prepared_token_counter=configured.count_prepared)
    compactor = _RecordingCompactor()
    # The local estimate is over budget, but the tokenizer counts the request
    # within it, so the rejection is diagnostic and history is not compacted.
    with pytest.raises(ProviderTokenCountError, match="invalid tool schema") as failed:
        await assembler.prepare(
            system_message={"content": "系统指令"}, conversation=_cjk_history(70),
            policy=ContextPolicy(compaction_strategy="chunked"), result_store=ResultStore(tmp_path, ".runtime"),
            enable_new_strategy=True, history_compactor=compactor,
        )
    assert not isinstance(failed.value, ProviderRenderRejected)
    assert compactor.calls == []


@pytest.mark.asyncio
async def test_candidates_far_beyond_the_local_overcount_are_not_sent_to_the_counter(tmp_path):
    from dagent.harness_runtime.context import ContextAssembler, HeuristicTokenCounter
    from dagent.harness_runtime.result_storage import ResultStore
    from dagent.schemas import ContextPolicy

    client = _CjkRenderClient()
    configured = provider(client, context_window_tokens=32768, max_output_tokens=4096)
    assembler = ContextAssembler(context_window_tokens=32768, max_output_tokens=4096,
                                 request_preparer=configured.prepare_call,
                                 prepared_token_counter=configured.count_prepared)
    compactor = _RecordingCompactor()
    prepared = await assembler.prepare(
        system_message={"content": "系统指令"}, conversation=_cjk_history(200),
        policy=ContextPolicy(compaction_strategy="chunked"), result_store=ResultStore(tmp_path, ".runtime"),
        enable_new_strategy=True, history_compactor=compactor,
    )
    assert compactor.calls
    assert prepared.usage.count_precision == "rendered"
    # Only the compacted request was counted; the original is ~4x the budget.
    assert [path for path, _ in client.raw_posts] == ["/v1/chat/completions/render"]
    counter = HeuristicTokenCounter()
    assert counter.count_text(json.dumps(client.raw_posts[0][1], ensure_ascii=False)) < 2 * (32768 - 4096)


@pytest.mark.asyncio
async def test_summary_split_tree_counts_only_plausible_chunks(tmp_path):
    from dagent.harness_runtime.compaction import HistoryCompactor
    from dagent.harness_runtime.context import ContextAssembler, HeuristicTokenCounter
    from dagent.harness_runtime.result_storage import ResultStore
    from dagent.schemas import AssistantMessage, ContextPolicy
    from tests.test_compaction_strategy import summary_response

    client = _CjkRenderClient()
    configured = provider(client, context_window_tokens=32768, max_output_tokens=4096)
    assembler = ContextAssembler(context_window_tokens=32768, max_output_tokens=4096,
                                 request_preparer=configured.prepare_call,
                                 prepared_token_counter=configured.count_prepared)

    async def chat(request):
        return summary_response(request)

    engine = HistoryCompactor(policy=ContextPolicy(compaction_strategy="chunked"), assembler=assembler,
                              result_store=ResultStore(tmp_path, ".runtime"), system_message={"content": "系统指令"},
                              chat=chat)
    await engine.compact(None, (AssistantMessage(id="source", content="中文内容" * 20000),), 4096)
    counter = HeuristicTokenCounter()
    rendered = [body for path, body in client.raw_posts if path.endswith("/render")]
    assert rendered
    for body in rendered:
        available = 32768 - body["max_completion_tokens"]
        assert counter.count_text(json.dumps(body, ensure_ascii=False)) <= 2 * available
