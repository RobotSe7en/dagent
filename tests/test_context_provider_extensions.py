"""Opt-in provider extensions preserve counted payloads and old defaults."""

from contextlib import closing
from dataclasses import replace
import json

import httpx
import pytest
from pydantic import ValidationError

import dagent
from dagent.providers import ChatResponse, MockProvider, ModelRequest, ProviderRequestError, ProviderReprepareRequired, ToolCall
from dagent.providers.model_io import ModelUserInput
from tests.test_cache_usage import _completion, _usage
from tests.test_reasoning_mapping import _endpoint
from examples.context_management import OfflineEndpoint, evidence
from examples.prepared_count_reprepare import RenderFallbackProvider
from tests.test_model_protocols import _openapi


class RepreparingProvider:
    """A host decides the policy; SDK owns replacement budgeting."""

    def __init__(self, provider, *, overflow=False):
        self.provider = provider
        self.omit = False
        self.overflow = overflow
        self.counted = set()
        self.sent = []

    def __getattr__(self, name):
        return getattr(self.provider, name)

    async def prepare_call(self, request, *, stream=False):
        request = replace(request, omit_reasoning_effort=self.omit)
        if self.omit and self.overflow:
            request = replace(request, instructions="mandatory " * 100000)
        return await self.provider.prepare_call(request, stream=stream)

    async def count_prepared(self, call):
        self.counted.add(call.fingerprint)
        return await self.provider.count_prepared(call)

    def rejected(self, error):
        if self.omit:
            raise error
        self.omit = True
        raise ProviderReprepareRequired(error.cause, metadata=error.metadata) from error

    async def complete_prepared(self, call):
        assert call.fingerprint in self.counted
        self.sent.append(call.payload)
        try:
            return await self.provider.complete_prepared(call)
        except ProviderRequestError as error:
            self.rejected(error)

    async def stream_prepared(self, call):
        assert call.fingerprint in self.counted
        self.sent.append(call.payload)
        try:
            async for event in self.provider.stream_prepared(call):
                yield event
        except ProviderRequestError as error:
            self.rejected(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_after_tool, fail_progress", [(False, False), (True, False), (True, True)])
async def test_render_count_reprepare_uses_host_policy_without_replaying_tools(tmp_path, reject_after_tool, fail_progress):
    tool_calls = []
    generations = []
    rendered = []

    @dagent.tool
    def remember(value: str) -> str:
        """Remember one value exactly once."""
        tool_calls.append(value)
        return "remembered"

    def respond(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/render"):
            rendered.append(body)
            if body.get("reasoning_effort") == "high" and (not reject_after_tool or tool_calls):
                return httpx.Response(400, json={"error": {"param": "reasoning_effort",
                    "code": "unsupported_value", "message": "Model rejects high effort"}})
            return httpx.Response(200, json={"token_ids": [1, 2, 3], "max_model_len": 4096})
        generations.append(body)
        response = _completion("chat_completions", False, usage=_usage("chat_completions"), content="done")
        if len(generations) == 1:
            data = response.json()
            data["choices"][0]["finish_reason"] = "tool_calls"
            data["choices"][0]["message"] = {"role": "assistant", "content": "", "tool_calls": [{
                "id": "remember-once", "type": "function", "function": {
                    "name": "tool_remember", "arguments": json.dumps({"value": "one"}),
                },
            }]}
            response = httpx.Response(200, json=data)
        return response

    spec = _openapi()
    spec["paths"]["/v1/chat/completions/render"] = {"post": {}}
    async with _endpoint(protocol="chat_completions", spec=spec, responder=respond,
        context_window_tokens=4096, max_output_tokens=512, reasoning_effort="high", token_counting="vllm",
    ) as (native, _posts):
        wrapper = RenderFallbackProvider(native)
        if fail_progress:
            # The host changed its strategy flag but failed to change the wire
            # request. The SDK must fail with the signal and retain paid tools.
            wrapper.prepare_call = native.prepare_call
        with closing(dagent.Runner(provider=wrapper, workspace=tmp_path)) as runner:
            agent = dagent.ToolAgent(
                profile="conversation", capabilities=[remember],
                context=dagent.ContextPolicy(compaction_strategy="cache_aware"),
            )
            if fail_progress:
                with pytest.raises(ProviderReprepareRequired) as failure:
                    await runner.run(agent, input="Remember one value, then finish.")
                result = failure.value.result
            else:
                result = await runner.run(agent, input="Remember one value, then finish.")
        assert native.config.reasoning_effort == "high"
    assert tool_calls == ["one"]
    if fail_progress:
        assert result.status == "failed"
        assert len(generations) == 1
        assert any(isinstance(item, dagent.ToolResultMessage) for item in result.conversation.items)
        return
    assert result.status == "completed"
    assert len(generations) == 2
    assert wrapper.high_rejected
    assert generations[-1] == rendered[-1] == wrapper.counted[-1].payload
    assert generations[-1]["reasoning_effort"] == "medium"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_explicit_omission_wins_over_defaults_mapping_and_overlays(protocol):
    def respond(request):
        return _completion(protocol, False, usage=_usage(protocol), content="done")

    async with _endpoint(protocol=protocol, responder=respond, reasoning_effort="high",
        reasoning_mapping={protocol: {"field": "custom.level", "values": {"high": "deep"}}},
        extra_body={"reasoning_effort": "high", "custom": {"level": "deep", "other": 1},
                    "reasoning": {"effort": "high", "summary": "concise"},
                    "chat_template_kwargs": {"enable_thinking": True}},
    ) as (provider, posts):
        request = ModelRequest(instructions="Be helpful.",
            items=(ModelUserInput(source_id="u", content="Hello"),), omit_reasoning_effort=True)
        original = provider.config.model_dump()
        response = await provider.complete(request)
        assert provider.config.model_dump() == original
    body = json.loads(posts[0].content)
    assert "reasoning_effort" not in body
    assert body["custom"] == {"other": 1}
    assert body["reasoning"] == {"summary": "concise"}
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert response.metadata.effective_reasoning_effort is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("overflow", [False, True])
async def test_reprepare_recounts_without_replaying_tools(tmp_path, stream, overflow):
    def respond(request):
        body = json.loads(request.content)
        if body.get("reasoning_effort"):
            return httpx.Response(400, json={"error": {"param": "reasoning_effort",
                "code": "unsupported_parameter", "message": "Unsupported parameter"}})
        return _completion("chat_completions", stream, usage=_usage("chat_completions"), content="done")

    async with _endpoint(protocol="chat_completions", token_counting="heuristic",
        reasoning_effort="high", responder=respond,
    ) as (native, posts):
        provider = RepreparingProvider(native, overflow=overflow)
        with closing(dagent.Runner(provider=provider, workspace=tmp_path,
                                  context_observability=True)) as runner:
            agent = dagent.ToolAgent(profile="conversation", capabilities=[],
                context=dagent.ContextPolicy(compaction_strategy="cache_aware"))
            async def run():
                if stream:
                    events = [event async for event in runner.stream(agent, input="Hello")]
                    assert events[-1].data.result is not None, str(events[-1].data)
                    return events[-1].data.result
                return await runner.run(agent, input="Hello")
            if overflow:
                if stream:
                    result = await run()
                    assert result is not None, "stream failure lost its SDK result"
                    assert result.status == "failed"
                else:
                    with pytest.raises(dagent.ContextWindowExceeded) as failure:
                        await run()
                    result = failure.value.result
                    assert result.context_usage[-1] == failure.value.usage
                assert result.context_usage[-1].estimated_input_tokens > result.context_usage[-1].input_budget_tokens
                assert result.context_usage[-2].estimated_input_tokens <= result.context_usage[-2].input_budget_tokens
                assert len(posts) == 1
            else:
                result = await run()
                assert result.status == "completed"
                assert len(posts) == 2
                assert "reasoning_effort" not in provider.sent[-1]


@pytest.mark.parametrize("payload", [
    {"type": "model_call_observed", "call_id": "call", "purpose": "generation",
     "status": "completed", "elapsed_seconds": 0.1},
    {"type": "context_compaction_progress", "compaction_id": "compaction", "received_characters": 10},
    {"type": "context_compaction_ended", "compaction_id": "compaction", "outcome": "model"},
])
def test_scoped_observations_handle_only_known_attribution_fields(payload):
    from dagent.runner import _stream_event_from_runtime

    scoped = {**payload, "run_id": "run", "parent_capability_id": "agent.helper",
              "dag_id": "dag", "node_id": "node"}
    event = _stream_event_from_runtime(scoped)
    assert event.run_id == "run"
    assert dagent.RunStreamEvent.model_validate(event.model_dump(mode="json")) == event
    # Keep strict validation for unexpected observation data.
    with pytest.raises(ValidationError):
        _stream_event_from_runtime({**scoped, "unexpected": True})


@pytest.mark.asyncio
@pytest.mark.parametrize("execution", ["subagent", "dag"])
async def test_scoped_agent_streams_complete_with_context_observability(tmp_path, execution):
    helper = dagent.ToolAgent(profile="conversation", name="helper", capabilities=[], skills=[], max_steps=1)
    responses = [ChatResponse(content="helper done")]
    if execution == "subagent":
        responses = [ChatResponse(tool_calls=[ToolCall(id="delegate", name="agent_helper",
                     arguments={"prompt": "Help"})]), *responses, ChatResponse(content="done")]
    with closing(dagent.Runner(provider=MockProvider(responses), workspace=tmp_path,
                              context_observability=True)) as runner:
        if execution == "subagent":
            runner.add_agent(helper)
            target = dagent.ToolAgent(profile="conversation", capabilities=[], skills=[], agents=["agent.helper"])
            events = [event async for event in runner.stream(target, input="Delegate")]
        else:
            target = dagent.Dag("observed")
            target.add_node(dagent.Node("helper", target=helper, inputs={"prompt": "Help"}))
            events = [event async for event in runner.stream(target, workspace_root=tmp_path / "runs")]
    assert events[-1].data.result.status == "completed"
    observations = [event for event in events if event.type == "model.call.observed"]
    assert len(observations) == (3 if execution == "subagent" else 1)
    assert all(event.data.status == "completed" for event in observations)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["cache_aware", "legacy"])
async def test_opt_in_observes_silent_summaries_without_exposing_text(tmp_path, strategy):
    endpoint = OfflineEndpoint()
    async with _endpoint(protocol="chat_completions", token_counting="heuristic",
        context_window_tokens=8192, max_output_tokens=512, responder=endpoint,
    ) as (provider, _posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, context_observability=True)) as runner:
            agent = dagent.ToolAgent(profile="conversation", capabilities=[evidence, "tool.read_file"],
                max_steps=32, context=dagent.ContextPolicy(compaction_strategy=strategy,
                    summary_max_tokens=512, max_tool_result_tokens=1024, allow_reference_fallback=True))
            events = [event async for event in runner.stream(agent, input="Collect 24 observations.")]
    assert events[-1].data.result.status == "completed"
    assert any(event.type == "context.compaction.started" for event in events)
    assert any(event.type == "context.compaction.progress" for event in events)
    assert any(event.type == "context.compaction.ended" for event in events)
    calls = [event for event in events if event.type == "model.call.observed"]
    assert {event.data.purpose for event in calls} == {"generation", "compaction"}
    assert len({event.data.call_id for event in calls}) == len(calls)
    for event in events:
        if event.type in {"context.compaction.progress", "context.compaction.ended", "model.call.observed"}:
            assert dagent.RunStreamEvent.model_validate(event.model_dump(mode="json")) == event
        if event.type in {"context.compaction.progress", "model.call.observed"}:
            assert "sample evidence" not in json.dumps(event.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_summary_reprepare_is_counted_and_bounded(tmp_path):
    endpoint = OfflineEndpoint()
    rejected = False

    def respond(request):
        nonlocal rejected
        body = json.loads(request.content)
        source = "\n".join(str(item.get("content", "")) for item in body["messages"])
        summary = "Sources to summarize:" in source or source.startswith("Produce a task checkpoint")
        if summary and not rejected:
            rejected = True
            return httpx.Response(400, json={"error": {"param": "reasoning_effort",
                "code": "unsupported_parameter", "message": "Unsupported parameter"}})
        return endpoint(request)

    async with _endpoint(protocol="chat_completions", token_counting="heuristic",
        context_window_tokens=8192, max_output_tokens=512, responder=respond,
    ) as (native, _posts):
        provider = RepreparingProvider(native)
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, context_observability=True)) as runner:
            agent = dagent.ToolAgent(profile="conversation", capabilities=[evidence, "tool.read_file"],
                max_steps=32, context=dagent.ContextPolicy(compaction_strategy="cache_aware",
                    summary_max_tokens=512, max_tool_result_tokens=1024, allow_reference_fallback=True))
            events = [event async for event in runner.stream(agent, input="Collect observations.")]
    assert rejected
    assert events[-1].data.result.status == "completed"
    calls = [event.data for event in events if event.type == "model.call.observed"
             and event.data.purpose == "compaction"]
    assert any(call.status == "failed" for call in calls)
    assert any(call.status == "completed" for call in calls)
    starts = {event.data.compaction_id for event in events if event.type == "context.compaction.started"}
    ends = [event.data.compaction_id for event in events if event.type == "context.compaction.ended"]
    assert starts == set(ends)
    assert len(ends) == len(starts)


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_reprepare_rejects_repeated_payload_or_partial_stream(partial):
    from dataclasses import dataclass
    from dagent.harness_runtime.model_observation import provider_events
    from dagent.providers.model_io import ModelStreamEvent

    @dataclass
    class Call:
        fingerprint: str = "same"

    class Provider:
        sent = 0

        async def stream_prepared(self, call):
            self.sent += 1
            if partial:
                yield ModelStreamEvent(type="token", content="already visible")
            raise ProviderReprepareRequired(ValueError("explicit rejection"),
                metadata=dagent.ModelCallMetadata(protocol="chat_completions"))

    provider = Provider()
    prepared = Call()
    reprepared = 0

    async def reprepare():
        nonlocal reprepared
        reprepared += 1
        return prepared

    with pytest.raises(ProviderReprepareRequired):
        async for _event in provider_events(provider, ModelRequest(instructions="", items=()), stream=True,
                                            prepared=prepared, reprepare=reprepare):
            pass
    assert provider.sent == 1
    assert reprepared == (0 if partial else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("response_error", [False, True])
async def test_failed_observation_retains_received_usage(response_error):
    from dagent.harness_runtime.model_observation import ContextObserver, provider_events
    from dagent.providers import ProviderResponseError
    from dagent.providers.model_io import ModelResponse

    metadata = dagent.ModelCallMetadata(protocol="responses", response_status="incomplete")
    response = ModelResponse(usage=dagent.ModelTokenUsage(input_tokens=50, cached_input_tokens=40),
                             metadata=metadata)
    error = (ProviderResponseError("incomplete", reason="output_limit", response=response)
             if response_error else ProviderRequestError(ValueError("interrupted"),
                                                         metadata=metadata, response=response))

    class Provider:
        async def complete(self, request):
            raise error

    events = []
    observer = ContextObserver(True, events.append, "r")
    with pytest.raises(type(error)):
        async for _event in provider_events(Provider(), ModelRequest(instructions="", items=()),
                                            stream=False, observer=observer):
            pass
    assert len(events) == 1
    assert events[0]["status"] == "failed"
    assert events[0]["usage"].cached_input_tokens == 40
    assert events[0]["model_call"] == metadata
