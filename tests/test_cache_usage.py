"""Opt-in cache statistics preserve generation, wire and persisted contracts."""

from contextlib import closing
import json

import httpx
import pytest
import yaml

import dagent
from dagent.config import (
    ProviderConfig, UserDagentConfig, UserModelProviderConfig,
    load_user_config, save_user_config,
)
from tests.planner_helpers import final_answer_response
from tests.test_reasoning_mapping import _endpoint, _invoke
from tests.test_request_reasoning import completion_body


def _usage(protocol, cached=80, *, input_tokens=1000):
    if protocol == "chat_completions":
        return {
            "prompt_tokens": input_tokens, "completion_tokens": 20,
            "total_tokens": (input_tokens or 0) + 20,
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens_details": {"reasoning_tokens": 5},
        }
    return {
        "input_tokens": input_tokens, "output_tokens": 20,
        "total_tokens": (input_tokens or 0) + 20,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens_details": {"reasoning_tokens": 5},
    }


def _completion(protocol, stream, *, usage, content="done", calls=(),
                reasoning="", status="completed", snapshots=None):
    body = completion_body(protocol)
    body["usage"] = usage
    if protocol == "chat_completions":
        choice = body["choices"][0]
        message = choice["message"]
        message["content"] = content
        if reasoning:
            message["reasoning"] = reasoning
        if calls:
            message["tool_calls"] = [{
                "id": call.id, "type": "function", "function": {
                    "name": call.name, "arguments": json.dumps(call.arguments),
                },
            } for call in calls]
        choice["finish_reason"] = (
            "length" if status == "incomplete" else "tool_calls" if calls else "stop"
        )
    else:
        body["status"] = status
        body["output"][0]["content"][0]["text"] = content
        if reasoning:
            body["output"].insert(0, {
                "type": "reasoning", "id": "rs_test", "summary": [],
                "content": [{"type": "reasoning_text", "text": reasoning}],
            })
        body["output"].extend({
            "type": "function_call", "id": f"fc_{call.id}", "call_id": call.id,
            "name": call.name, "arguments": json.dumps(call.arguments),
        } for call in calls)
    if not stream:
        return httpx.Response(200, json=body)
    if protocol == "chat_completions":
        delta = dict(message)
        if calls:
            delta["tool_calls"] = [
                {"index": index, **call} for index, call in enumerate(message["tool_calls"])
            ]
        chunk = {key: body[key] for key in ("id", "created", "model")}
        chunk["object"] = "chat.completion.chunk"
        events = [{**chunk, "choices": [{
            "index": 0, "finish_reason": choice["finish_reason"], "delta": delta,
        }]}]
        events.extend({**chunk, "choices": [], "usage": snapshot}
                      for snapshot in (snapshots if snapshots is not None else [usage]))
    else:
        events = [{
            "type": f"response.{status}", "response": body, "sequence_number": 1,
        }]
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        text="".join(f"data: {json.dumps(event)}\n\n" for event in events)
        + "data: [DONE]\n\n",
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_cache_usage_config_round_trips_and_reaches_runner(tmp_path, enabled):
    assert ProviderConfig(base_url="http://model.test/v1", model="test").capture_cache_usage is False
    provider = UserModelProviderConfig(
        base_url="http://model.test/v1", model="test", capture_cache_usage=enabled,
    )
    user_path = tmp_path / "user.yaml"
    save_user_config(UserDagentConfig(model_providers={"test": provider}), user_path)
    saved = yaml.safe_load(user_path.read_text())["model_providers"]["test"]
    assert ("capture_cache_usage" in saved) is enabled
    assert load_user_config(user_path).model_providers["test"].capture_cache_usage is enabled
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"provider": saved}))
    with closing(dagent.Runner.from_config(path, workspace=tmp_path, skill_roots=[])) as runner:
        assert runner.runtime.provider.config.capture_cache_usage is enabled


@pytest.mark.parametrize("cached", [None, 0, 80])
def test_cache_usage_serialization_preserves_old_records(cached):
    old = {"input_tokens": 1000, "output_tokens": 20, "reasoning_tokens": None, "total_tokens": 1020}
    usage = dagent.ModelTokenUsage(**old, cached_input_tokens=cached)
    expected = old if cached is None else {**old, "cached_input_tokens": cached}
    assert usage.model_dump() == expected
    assert json.loads(usage.model_dump_json()) == expected
    assert usage.model_dump(include={"cached_input_tokens"}) == (
        {} if cached is None else {"cached_input_tokens": cached}
    )
    assert usage.model_dump(exclude={"cached_input_tokens"}) == old
    conversation = dagent.ConversationState(
        items=(dagent.AssistantMessage(usage=usage),),
        summary=dagent.ContextSummary(content="summary", source_item_count=1, usage=usage),
    )
    payload = json.loads(conversation.model_dump_json())
    assert payload["items"][0]["usage"] == payload["summary"]["usage"] == expected
    assert dagent.ConversationState.model_validate_json(conversation.model_dump_json()) == conversation
    assert dagent.ModelTokenUsage.model_validate(old).cached_input_tokens is None


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("cached", [None, 0, 80])
async def test_cache_usage_standard_fields(protocol, stream, cached):
    def responder(request):
        return _completion(protocol, stream, usage=_usage(protocol, cached))

    async with _endpoint(
        protocol=protocol, capture_cache_usage=True, stream_include_usage=True,
        responder=responder,
    ) as (provider, posts):
        response = await _invoke(provider, stream)
        assert provider.config.capture_cache_usage is True
    assert response.content == "done"
    assert response.usage == dagent.ModelTokenUsage(
        input_tokens=1000, output_tokens=20, reasoning_tokens=5, total_tokens=1020,
        cached_input_tokens=cached,
    )
    assert "capture_cache_usage" not in json.loads(posts[-1].content)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
async def test_cache_usage_capture_does_not_change_wire(protocol, stream, legacy, caplog):
    def responder(request):
        actual_protocol = "responses" if request.url.path.endswith("/responses") else "chat_completions"
        return _completion(actual_protocol, stream, usage=_usage(actual_protocol, -1))

    async with _endpoint(protocol=protocol, responder=responder) as (provider, posts):
        default = await _invoke(provider, stream, legacy=legacy)
        provider.config.capture_cache_usage = True
        enabled = await _invoke(provider, stream, legacy=legacy)
    assert json.loads(posts[0].content) == json.loads(posts[1].content)
    assert "stream_options" not in json.loads(posts[0].content)
    assert default.usage.cached_input_tokens is enabled.usage.cached_input_tokens is None
    assert len(caplog.records) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("value", [-1, True, "80", 1.5, {}, [], 1001])
async def test_cache_usage_invalid_statistics_do_not_fail(protocol, stream, value, caplog):
    async with _endpoint(
        protocol=protocol, capture_cache_usage=True,
        responder=lambda request: _completion(protocol, stream, usage=_usage(protocol, value)),
    ) as (provider, _posts):
        response = await _invoke(provider, stream)
    assert response.content == "done"
    assert response.usage.input_tokens == 1000
    assert response.usage.reasoning_tokens == 5
    assert response.usage.total_tokens == 1020
    assert response.usage.cached_input_tokens is None
    assert len(caplog.records) == 1
    assert protocol in caplog.text and "cached_tokens" in caplog.text
    assert "synthetic" not in caplog.text and "done" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("missing", ["usage", "details", "field", "other_protocol", "private"])
async def test_cache_usage_missing_is_unknown_without_warning(protocol, missing, caplog):
    usage = _usage(protocol)
    field = "prompt_tokens_details" if protocol == "chat_completions" else "input_tokens_details"
    if missing == "usage":
        usage = None
    elif missing == "details":
        usage[field] = None
    elif missing == "field":
        usage[field] = {}
    else:
        usage.pop(field)
        if missing == "other_protocol":
            other = "input_tokens_details" if protocol == "chat_completions" else "prompt_tokens_details"
            usage[other] = {"cached_tokens": 80}
        else:
            usage["prompt_cache_hit_tokens"] = 80
    async with _endpoint(
        protocol=protocol, capture_cache_usage=True,
        responder=lambda request: _completion(protocol, False, usage=usage),
    ) as (provider, _posts):
        response = await _invoke(provider, False)
    if missing == "usage":
        assert response.usage is None
    else:
        assert response.usage.cached_input_tokens is None
    assert not caplog.records


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [None, 0, 80])
async def test_cache_usage_stream_snapshots_are_not_accumulated(cached):
    async with _endpoint(
        protocol="chat_completions", capture_cache_usage=True,
        responder=lambda request: _completion(
            "chat_completions", True, usage=None,
            snapshots=[_usage("chat_completions", 30), _usage("chat_completions", cached)],
        ),
    ) as (provider, _posts):
        response = await _invoke(provider, True)
    assert response.usage.cached_input_tokens == cached
    assert response.usage.input_tokens == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_cache_usage_can_be_known_without_input_total(protocol):
    async with _endpoint(
        protocol=protocol, capture_cache_usage=True,
        responder=lambda request: _completion(protocol, False, usage=_usage(protocol, input_tokens=None)),
    ) as (provider, _posts):
        response = await _invoke(provider, False)
    assert response.usage.input_tokens is None
    assert response.usage.cached_input_tokens == 80


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_cache_usage_incomplete_response_keeps_usage(protocol, stream):
    from dagent.providers import ProviderResponseError

    async with _endpoint(
        protocol=protocol, capture_cache_usage=True,
        responder=lambda request: _completion(
            protocol, stream, usage=_usage(protocol), content="partial", status="incomplete",
        ),
    ) as (provider, _posts):
        with pytest.raises(ProviderResponseError) as caught:
            await _invoke(provider, stream)
    assert caught.value.response.content == "partial"
    assert caught.value.response.usage.cached_input_tokens == 80


@pytest.mark.asyncio
async def test_cache_usage_failed_responses_stream_retains_reported_usage():
    from dagent.providers import ProviderResponseError

    async with _endpoint(
        protocol="responses", capture_cache_usage=True,
        responder=lambda request: _completion(
            "responses", True, usage=_usage("responses"), content="partial", status="failed",
        ),
    ) as (provider, _posts):
        with pytest.raises(ProviderResponseError) as caught:
            await _invoke(provider, True)
    assert caught.value.response.status == "failed"
    assert caught.value.response.content == "partial"
    assert caught.value.response.usage.cached_input_tokens == 80


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_cache_usage_retry_retains_only_returned_response_usage(protocol):
    from dagent.harness_runtime.llm_retry import LLMRetryPolicy, run_with_llm_retries

    calls = 0

    def responder(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={
                "error": {"message": "busy", "type": "server_error"},
                "usage": _usage(protocol, 900),
            })
        return _completion(protocol, False, usage=_usage(protocol, 80))

    async with _endpoint(
        protocol=protocol, capture_cache_usage=True, responder=responder,
    ) as (provider, _posts):
        response = await run_with_llm_retries(
            lambda: _invoke(provider, False),
            policy=LLMRetryPolicy(max_retries=1, retry_delays_seconds=(0,)),
        )
    assert calls == 2
    assert response.usage.cached_input_tokens == 80
    assert response.usage.total_tokens == 1020
    assert [attempt.http_status for attempt in response.metadata.attempts] == [503, 200]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_cache_usage_stream_interruption_does_not_invent_usage(protocol):
    from dagent.providers import ProviderRequestError

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            if protocol == "chat_completions":
                events = [
                    {"id": "chat", "object": "chat.completion.chunk", "model": "test", "created": 0,
                     "choices": [{"index": 0, "delta": {"content": "partial"}}]},
                    {"id": "chat", "object": "chat.completion.chunk", "model": "test", "created": 0,
                     "choices": [], "usage": _usage(protocol)},
                ]
            else:
                events = [{"type": "response.output_text.delta", "sequence_number": 1,
                           "item_id": "msg", "output_index": 0, "content_index": 0, "delta": "partial"}]
            for event in events:
                yield f"data: {json.dumps(event)}\n\n".encode()
            raise httpx.ReadTimeout("interrupted")

    async with _endpoint(
        protocol=protocol, capture_cache_usage=True,
        responder=lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Interrupted(),
        ),
    ) as (provider, _posts):
        with pytest.raises(ProviderRequestError) as caught:
            await _invoke(provider, True)
    response = caught.value.response
    assert response.content == "partial"
    if protocol == "chat_completions":
        assert response.usage.cached_input_tokens == 80
    else:
        assert response.usage is None


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", [False, True])
async def test_cache_usage_preserves_responses_nonterminal_events(capture):
    body = completion_body("responses")
    body["usage"] = _usage("responses")
    body["output"].append({
        "type": "function_call", "id": "fc", "call_id": "call",
        "name": "lookup", "arguments": '{"key":"value"}',
    })
    events = [
        {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0,
         "item": {"type": "function_call", "id": "fc", "call_id": "call",
                  "name": "lookup", "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "sequence_number": 2,
         "item_id": "fc", "output_index": 0, "delta": '{"key":"value"}'},
        {"type": "response.output_text.delta", "sequence_number": 3,
         "item_id": "msg_1", "output_index": 1, "content_index": 0, "delta": "done"},
        {"type": "response.completed", "sequence_number": 4, "response": body},
    ]
    observed = []
    async with _endpoint(
        protocol="responses", capture_cache_usage=capture,
        responder=lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            text="".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n",
        ),
    ) as (provider, _posts):
        original = provider._read_model_stream

        async def record(source, partial):
            async for event in original(source, partial):
                if event.type == "response.output_item.added":
                    observed.append((event.item.type, event.item.call_id, event.item.name))
                yield event

        provider._read_model_stream = record
        response = await _invoke(provider, True)
    assert observed == [("function_call", "call", "lookup")]
    assert response.content == "done"
    assert response.tool_calls[0].id == "call"
    assert response.tool_calls[0].arguments == {"key": "value"}
    assert response.usage.cached_input_tokens == (80 if capture else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("target", ["tool", "dag"])
async def test_cache_usage_reaches_results_trace_and_checkpoint(tmp_path, protocol, stream, target):
    content = final_answer_response("done") if target == "dag" else "done"
    async with _endpoint(
        protocol=protocol, token_counting="heuristic", capture_cache_usage=True,
        stream_include_usage=True,
        responder=lambda request: _completion(protocol, stream, usage=_usage(protocol), content=content),
    ) as (provider, _posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            agent = dagent.DagAgent(capabilities=[]) if target == "dag" else dagent.ToolAgent(profile="conversation", capabilities=[])
            if stream:
                events = [event async for event in runner.stream(agent, input="hello")]
                result = events[-1].data.result
            else:
                result = await runner.run(agent, input="hello")
    assert result.status == "completed"
    messages = [item for item in result.new_items if isinstance(item, dagent.AssistantMessage) and item.usage]
    assert messages and all(item.usage.cached_input_tokens == 80 for item in messages)
    restored = dagent.RunCheckpoint.model_validate_json(result.checkpoint.model_dump_json())
    thread = restored.state.model_thread
    assert any(isinstance(item, dagent.AssistantMessage) and item.usage.cached_input_tokens == 80
               for item in thread.items if isinstance(item, dagent.AssistantMessage) and item.usage)
    if target == "tool":
        model_calls = [node for node in result.trace.root.children if node.kind == "model_call"]
        assert model_calls[0].output["usage"]["cached_input_tokens"] == 80
        assert dagent.RunResult.model_validate(result.model_dump(mode="json")).conversation == result.conversation


@pytest.mark.asyncio
async def test_cache_usage_compaction_retains_its_own_usage(tmp_path):
    calls = 0

    def responder(request):
        nonlocal calls
        calls += 1
        return _completion("chat_completions", False, usage=_usage("chat_completions", calls),
                           content="bounded summary" if calls == 2 else "done")

    async with _endpoint(
        protocol="chat_completions", token_counting="heuristic", capture_cache_usage=True,
        context_window_tokens=2048, max_output_tokens=256, responder=responder,
    ) as (provider, _posts):
        agent = dagent.ToolAgent(profile="conversation", capabilities=[], context=dagent.ContextPolicy(
            compaction_trigger_ratio=0.2, summary_max_tokens=64,
        ))
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            first = await runner.run(agent, input="x" * 600)
            result = await runner.run(agent, input="y" * 600, conversation=first.conversation)
    assert calls == 3 and result.status == "completed"
    assert result.conversation.summary.usage.cached_input_tokens == 2
    assert result.conversation.summary.model_call.request_purpose == "compaction"
    assert result.conversation.items[-1].usage.cached_input_tokens == 3
