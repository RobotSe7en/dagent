"""Response retries consume model steps without replaying successful effects."""

import asyncio
from contextlib import closing
from dataclasses import fields
import json

import httpx
import pytest

import dagent
from dagent.harness_runtime.context import compaction_source
from dagent.harness_runtime.context_strategy import _source_key
from dagent.harness_runtime.compaction import _source_texts
from dagent.providers import ChatResponse, ChatStreamEvent, MockProvider, ProviderResponseError, ToolCall
from dagent.providers.model_io import ModelResponse
from dagent.result import ResponseDiscardedData
from tests.test_conversation_changes import Host, invoke, consume, resume_invoke
from tests.test_reasoning_mapping import _endpoint
from tests.test_cache_usage import _completion, _usage
from tests.test_compaction_strategy import checkpoint, request_aliases


class FaultProvider(MockProvider):
    async def chat(self, *args, **kwargs):
        value = await super().chat(*args, **kwargs)
        if isinstance(value, Exception):
            raise value
        return value


def failure(reason):
    if reason == "empty_response":
        return ChatResponse()
    if reason == "reasoning_only_response":
        return ChatResponse(reasoning_content="private thought")
    if reason == "incomplete":
        return ChatResponse(content="partial", status="incomplete")
    if reason == "duplicate":
        return ChatResponse(tool_calls=[ToolCall(id="same", name="tool_echo")] * 2)
    return ProviderResponseError("failed" if reason == "invalid_tool_call" else "incomplete",
        reason=reason, response=ModelResponse(content="partial", reasoning="private thought", status="incomplete"))


@pytest.mark.parametrize("agent_type", [dagent.ToolAgent, dagent.AutoAgent])
@pytest.mark.parametrize("value", [-1, True, 1.5, "1", None])
def test_response_retry_configuration_is_strict(agent_type, value):
    with pytest.raises(ValueError, match="response_retries"):
        agent_type(profile="conversation", response_retries=value)
    assert fields(agent_type)[-1].name == "response_retries"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("after", ["user", "tool"])
@pytest.mark.parametrize("reason", ["empty_response", "reasoning_only_response", "max_output_tokens",
    "missing_finish_reason", "missing_terminal_response", "invalid_tool_call", "incomplete", "duplicate"])
async def test_retryable_response_recovers_and_continues(tmp_path, stream, after, reason):
    @dagent.tool
    def echo() -> str:
        return "evidence"

    responses = [failure(reason), ChatResponse(content="recovered"), ChatResponse(content="continued")]
    if after == "tool":
        responses.insert(0, ChatResponse(tool_calls=[ToolCall(id="evidence", name="tool_echo")]))
    first = 0 if after == "user" else 1
    provider = FaultProvider(responses)
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        result, events = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[echo], response_retries=1),
                                      host, stream, input="finish")
        assert result.status == "completed" and result.output_text == "recovered"
        assert host.state == result.conversation
        assert [item.content for item in result.conversation.items
                if item.type == "assistant" and not item.tool_calls] == ["recovered"]
        hints = [item for item in result.conversation.items if getattr(item, "purpose", None) == "response_retry"]
        failed_request, retry_request = provider.requests[first], provider.requests[first + 1]
        if after == "user":
            # A hint here would make two user turns in a row, which role-alternating
            # chat templates reject; the identical request is resent instead.
            assert hints == [] and retry_request["messages"] == failed_request["messages"]
        else:
            assert len(hints) == 1 and hints[0].visibility == "internal" and hints[0].run_id == result.run_id
            assert "private thought" not in hints[0].content and "partial" not in hints[0].content
            assert retry_request["messages"][:-1] == failed_request["messages"]
            assert retry_request["messages"][-1]["role"] == "user" and "purpose" not in retry_request["messages"][-1]
        roles = [message["role"] for message in retry_request["messages"]]
        assert not any(left == right == "user" for left, right in zip(roles, roles[1:]))
        outcomes = [o.status for b in host.batches for o in b.observations
                    if o.type == "response" and o.status != "started"]
        assert outcomes == ["completed"] * first + ["failed", "completed"]
        assert result.usage.model_turns == first + 2
        discarded = [record for batch in host.batches for record in batch.messages
                     if record.item.type == "assistant" and record.partial]
        assert len(discarded) == 1
        assert discarded[0].item.id not in {item.id for item in result.conversation.items}
        if stream:
            event = next(event for event in events if event.type == "response.discarded")
            assert isinstance(event.data, ResponseDiscardedData)
            assert event.data.retry == 1 and event.data.model_step == first + 1
            assert event.data.message_id == discarded[0].item.id
            assert dagent.RunStreamEvent.model_validate(event.model_dump(mode="json")) == event
            boundaries = [event.type for event in events if event.type in {
                "response.started", "response.finished", "response.discarded"}]
            assert boundaries == ["response.started", "response.finished"] * first + [
                "response.started", "response.finished", "response.discarded", "response.started", "response.finished"]
            finished = [e for e in events if e.type == "response.finished"][first]
            assert finished.data.response_id == event.data.response_id
        second, _ = await invoke(runner, dagent.ToolAgent(profile="conversation"), host, stream,
                                input="continue", conversation=result.conversation)
        assert second.status == "completed" and host.state == second.conversation


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["content_filter", "refusal", "unknown_error", "provider_request_failed", "failed"])
async def test_structured_nonretryable_error_wins_over_incomplete(tmp_path, reason):
    bad = (ChatResponse(status="failed") if reason == "failed" else
           ProviderResponseError("incomplete", reason=reason, response=ModelResponse(status="incomplete")))
    provider = FaultProvider([bad, ChatResponse(content="must not request")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = await runner.run(dagent.ToolAgent(profile="conversation", response_retries=5), input="finish")
    assert result.status == "failed" and len(provider.requests) == 1
    assert not any(getattr(item, "purpose", None) for item in result.new_items)


@pytest.mark.asyncio
@pytest.mark.parametrize("retries,steps,requests", [(0, 10, 1), (2, 10, 3), (5, 1, 1), (5, 2, 2)])
async def test_exhaustion_keeps_error_and_empty_terminal_message(tmp_path, retries, steps, requests):
    provider = MockProvider([ChatResponse()] * 10)
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = await runner.run(dagent.ToolAgent(profile="conversation", response_retries=retries, max_steps=steps), input="finish")
    assert len(provider.requests) == requests
    assert result.status == "failed" and result.error.code == "empty_response"
    assert result.conversation.items[-1].type == "assistant" and result.conversation.items[-1].content == ""
    assert len([i for i in result.new_items if i.type == "assistant"]) == 1


@pytest.mark.asyncio
async def test_exhausted_failed_tool_calls_are_diagnostics_only(tmp_path):
    calls = [ToolCall(id="unsafe", name="tool_echo", arguments={"secret": "value"})]
    provider = MockProvider([ChatResponse(status="incomplete", tool_calls=calls), ChatResponse(content="next")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = await runner.run(dagent.ToolAgent(profile="conversation", response_retries=1, max_steps=1), input="finish")
        assert not result.conversation.items[-1].tool_calls
        assert result.trace.root.children[-1].output["provider_details"]["tool_calls"][0]["id"] == "unsafe"
        continued = await runner.run(dagent.ToolAgent(profile="conversation"), input="continue", conversation=result.conversation)
        assert continued.status == "completed"
        assert all(not message.get("tool_calls") for message in provider.requests[-1]["messages"])


@pytest.mark.asyncio
async def test_valid_tool_round_resets_retry_budget_without_replay(tmp_path):
    effects = []

    @dagent.tool
    def echo(index: int) -> str:
        effects.append(index)
        return "ok"

    provider = MockProvider([
        ChatResponse(), ChatResponse(tool_calls=[ToolCall(id="one", name="tool_echo", arguments={"index": 1})]),
        ChatResponse(), ChatResponse(tool_calls=[ToolCall(id="two", name="tool_echo", arguments={"index": 2})]),
        ChatResponse(), ChatResponse(content="done"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[echo], response_retries=1), input="finish")
    assert result.status == "completed" and effects == [1, 2]
    assert len(provider.requests) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("kind", ["empty", "reasoning", "duplicate", "length"])
async def test_builtin_provider_response_retries(tmp_path, stream, protocol, kind):
    count = 0

    def respond(request):
        nonlocal count
        count += 1
        requested_stream = json.loads(request.content).get("stream", False)
        bad = count == 1
        reply = _completion(protocol, requested_stream, usage=_usage(protocol),
            content="partial" if bad and kind == "length" else "" if bad else "done",
            reasoning="thought" if bad and kind == "reasoning" else "",
            calls=[ToolCall(id="duplicate", name="tool_echo")] * 2 if bad and kind == "duplicate" else (),
            status="incomplete" if bad and kind == "length" else "completed")
        if bad and kind == "length" and protocol == "responses":
            if requested_stream:
                event = json.loads(reply.text.split("data: ")[1].split("\n")[0])
                event["response"]["incomplete_details"] = {"reason": "max_output_tokens"}
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n")
            body = reply.json()
            body["incomplete_details"] = {"reason": "max_output_tokens"}
            return httpx.Response(200, json=body)
        return reply

    async with _endpoint(protocol=protocol, token_counting="heuristic", responder=respond) as (provider, posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            host = Host(runner=runner)
            result, _ = await invoke(runner, dagent.ToolAgent(profile="conversation", response_retries=1), host, stream, input="finish")
            assert result.status == "completed" and len(posts) == 2


def test_recovery_purpose_serialization_source_identity_and_labels():
    ordinary = dagent.UserMessage(id="same", run_id="run", content="hint", visibility="internal")
    hint = ordinary.model_copy(update={"purpose": "response_retry"})
    assert "purpose" not in ordinary.model_dump()
    assert dagent.UserMessage.model_validate_json(hint.model_dump_json()) == hint
    assert _source_key(ordinary, {}) != _source_key(hint, {})
    assert _source_key(ordinary, {}) == _source_key(ordinary.model_copy(update={"purpose": None}), {})
    for text in (compaction_source(None, (hint,)), _source_texts(None, (hint,))[hint.id]):
        assert "Runtime response recovery hint" in text and "not a lasting task requirement" in text
    assert compaction_source(None, (ordinary,)).startswith("User:\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_observation_must_be_acknowledged_before_retry(tmp_path, cancel):
    provider = MockProvider([ChatResponse(), ChatResponse(content="must not generate")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        stream = runner.stream(dagent.ToolAgent(profile="conversation", response_retries=1), input="finish",
                               run_id="failure", persistence=dagent.StreamPersistenceOptions())
        async for event in stream:
            if event.type == "conversation.changed":
                if any(r.partial for r in event.data.messages):
                    if cancel:
                        await stream.aclose()
                        break
                    assert await runner.reject_changes(event.run_id, event.data.batch_id, message="unavailable")
                else:
                    await runner.acknowledge_changes(event.run_id, event.data.batch_id)
        if not cancel:
            assert event.type == "run.failed" and event.data.error_type == "PersistenceRejected"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_partial_stream_order_across_observation_discard_and_hint(tmp_path):
    order = []

    @dagent.tool
    def echo() -> str:
        return "evidence"

    class PartialProvider(MockProvider):
        partial = False
        async def stream_chat(self, *args, **kwargs):
            # The second response breaks off after a tool result, so a hint follows.
            if len(self.requests) == 1 and not self.partial:
                self.partial = True
                yield ChatStreamEvent(type="token", content="temporary text")
                return
            async for event in super().stream_chat(*args, **kwargs):
                yield event
    provider = PartialProvider([ChatResponse(tool_calls=[ToolCall(id="evidence", name="tool_echo")]),
                                ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        async for event in runner.stream(dagent.ToolAgent(profile="conversation", capabilities=[echo], response_retries=1),
                                         input="finish",
                                         persistence=dagent.StreamPersistenceOptions()):
            if event.type == "conversation.changed":
                await host.save(event.data)
                if any(r.partial for r in event.data.messages):
                    order.append("observation")
                if any(getattr(r.item, "purpose", None) for r in event.data.messages):
                    order.append("hint")
                await runner.acknowledge_changes(event.run_id, event.data.batch_id)
            else:
                order.append(event.type)
        selected = [i for i in order if i in {"response.started", "response.finished", "response.discarded", "observation", "hint"}]
        assert selected == ["response.started", "response.finished",
                            "response.started", "response.finished", "observation", "response.discarded", "hint",
                            "response.started", "response.finished"]


@pytest.mark.asyncio
async def test_cancel_during_retry_hint_stops_generation(tmp_path):
    reached, release = asyncio.Event(), asyncio.Event()

    @dagent.tool
    def echo() -> str:
        return "evidence"

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="evidence", name="tool_echo")]),
                             ChatResponse(), ChatResponse(content="must not generate")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        original = host.save
        async def save(batch):
            if any(getattr(r.item, "purpose", None) for r in batch.messages):
                reached.set()
                await release.wait()
            await original(batch)
        host.save = save
        task = asyncio.create_task(invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[echo],
                                                                   response_retries=1), host,
                                         True, input="finish", run_id="retry_hint"))
        await asyncio.wait_for(reached.wait(), 5)
        assert await runner.cancel("retry_hint")
        release.set()
        result, _ = await asyncio.wait_for(task, 5)
        assert result.status == "cancelled"
    assert len(provider.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", [dagent.ToolAgent, dagent.AutoAgent])
async def test_checkpoint_freezes_retry_configuration_and_resets_at_review(tmp_path, agent_type):
    effects = []

    @dagent.tool(risk="medium")
    def record() -> str:
        effects.append("written")
        return "ok"

    provider = MockProvider([ChatResponse(), ChatResponse(tool_calls=[ToolCall(id="write", name="tool_record")]),
                             ChatResponse(), ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        first = await runner.run(agent_type(profile="conversation", capabilities=[record], review="careful", response_retries=1), input="record")
        assert first.requires_review and effects == []
        saved = dagent.RunCheckpoint.model_validate_json(first.checkpoint.model_dump_json())
        assert saved.plan.response_retries == 1
        assert saved.plan.model_dump()["response_retries"] == 1
        plan_without = dagent.ResolvedRunPlan.model_validate({**saved.plan.model_dump(), "response_retries": 0, "fingerprint": ""})
        assert "response_retries" not in plan_without.model_dump()
        assert saved.plan.fingerprint != plan_without.fingerprint
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[], capabilities=[record])) as runner:
        result = await runner.resume(first.review.approve(), checkpoint=saved)
        assert result.status == "completed" and effects == ["written"]
        assert result.plan.response_retries == 1
        assert all(item.id not in {old.id for old in first.new_items} for item in result.new_items)


@pytest.mark.asyncio
@pytest.mark.parametrize("child_retries", [0, 1])
async def test_subagent_uses_its_own_setting_and_fingerprint(tmp_path, child_retries):
    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="delegate", name="agent_helper", arguments={"prompt": "help"})]),
        ChatResponse(), *([ChatResponse(content="child recovered")] if child_retries else []), ChatResponse(content="parent done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        zero = runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[]))
        runner.remove_capability("agent.helper")
        definition = runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[], response_retries=child_retries))
        assert (zero.config["execution_fingerprint"] != definition.config["execution_fingerprint"]) == bool(child_retries)
        host = Host(runner=runner)
        result, events = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[], agents=["agent.helper"], response_retries=2),
                                      host, True, input="delegate")
    assert result.status == "completed"
    discards = [event for event in events if event.type == "response.discarded"]
    assert len(discards) == child_retries
    if discards:
        assert discards[0].data.parent_capability_id == "agent.helper"


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["legacy", "chunked", "cache_aware"])
async def test_three_hundred_tools_with_retries_compaction_and_review(tmp_path, strategy):
    effects = []
    next_page = 1
    failed_page = None
    summaries = 0
    recovery_sources = 0
    previous_messages = None
    previous_summaries = 0

    @dagent.tool
    def query(index: int) -> str:
        effects.append(index)
        return f"page {index} verified; " + "evidence " * 100

    @dagent.tool(risk="medium")
    def gate(index: int) -> str:
        effects.append(index)
        return f"page {index} verified; " + "evidence " * 100

    def respond(request):
        nonlocal next_page, failed_page, summaries, recovery_sources, previous_messages, previous_summaries
        body = json.loads(request.content)
        messages = body["messages"]
        text = str(messages)
        if "Sources to summarize:" in text or messages[0]["content"].startswith("Produce a task checkpoint"):
            summaries += 1
            recovery_sources += "Runtime response recovery hint (not a lasting task requirement)" in text
            aliases = request_aliases(text)
            content = checkpoint(requirements=[("Collect pages.", [aliases[0]])], next_step=[("Continue.", [aliases[-1]])])
        elif messages[0]["content"].startswith("Summarize earlier conversation data"):
            summaries += 1
            recovery_sources += "Runtime response recovery hint (not a lasting task requirement)" in text
            content = "Collected earlier pages. Continue collecting."
        else:
            if strategy == "cache_aware" and previous_messages is not None and previous_summaries == summaries:
                assert messages[:len(previous_messages)] == previous_messages
            previous_messages, previous_summaries = messages, summaries
            if next_page % 7 == 0 and failed_page != next_page:
                failed_page = next_page
                return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"), content="")
            page = next_page
            next_page += 1
            return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"),
                content="done" if page > 300 else "", calls=() if page > 300 else (
                    ToolCall(id=f"call_{page}", name="tool_gate" if page % 75 == 0 else "tool_query", arguments={"index": page}),))
        return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"), content=content)

    async with _endpoint(protocol="chat_completions", token_counting="heuristic", context_window_tokens=8192,
                         max_output_tokens=512, responder=respond) as (provider, _posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[], max_retained_runs=0)) as runner:
            host = Host(runner=runner)
            agent = dagent.ToolAgent(profile="conversation", capabilities=[query, gate], response_retries=1, review="careful",
                context=dagent.ContextPolicy(compaction_strategy=strategy, summary_max_tokens=512))
            result, _ = await invoke(runner, agent, host, False, input="Collect pages.")
            reviews = 0
            while result.requires_review:
                reviews += 1
                assert runner.run_checkpoint(result.run_id) is None
                saved = dagent.RunCheckpoint.model_validate_json(result.checkpoint.model_dump_json())
                assert saved.plan.response_retries == 1
                result, _ = await resume_invoke(runner, result.review.approve(), saved, host)
            assert result.status == "completed" and reviews == 4
            assert runner.run_state(result.run_id) is None
            assert runner.run_checkpoint(result.run_id) is None
            assert effects == list(range(1, 301))
            assert summaries > 5 and recovery_sources > 0
            assert host.state == result.conversation
            hints = [record.item for batch in host.batches for record in batch.messages if getattr(record.item, "purpose", None)]
            assert len(hints) >= 42
            assert len([item for item in result.conversation.items if getattr(item, "purpose", None)]) < 10
            assert any(usage.compacted_active_run_items for usage in result.context_usage)
