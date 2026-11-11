"""Public result, event and continuation contracts without snapshot files."""

from contextlib import closing
import json

import pytest

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall
from tests.test_cache_usage import _completion, _usage
from tests.test_compaction_strategy import checkpoint, request_aliases
from tests.test_reasoning_mapping import _endpoint


async def invoke(runner, agent, stream, **kwargs):
    if stream:
        events = [event async for event in runner.stream(agent, **kwargs)]
        return events[-1].data.result, events
    return await runner.run(agent, **kwargs), []


def assert_public_result(runner, result, events):
    assert result.kind == "tool"
    assert result.trace is not None
    assert result.conversation.schema_version == 4
    assert result.state.schema_version == 6
    assert runner.run_state(result.run_id) == result.state

    payload = result.model_dump(mode="json")
    assert set(payload) == {"state", "output_text", "new_items", "usage", "output_value"}
    assert dagent.RunResult.model_validate(payload).model_dump(mode="json") == payload
    saved = dagent.RunCheckpoint.model_validate_json(result.checkpoint.model_dump_json())
    assert saved.schema_version == 9
    assert saved.state == result.state
    assert saved.plan == result.plan
    assert saved.usage == result.usage
    assert runner.run_checkpoint(result.run_id) == saved

    if events:
        assert events[0].type == "run.started"
        assert events[-1].type == "run.finished"
        assert events[-1].data.result.model_dump(mode="json") == payload
        assert "conversation.changed" not in {event.type for event in events}
        for event in events:
            wire = event.model_dump(mode="json")
            assert dagent.RunStreamEvent.model_validate(wire).model_dump(mode="json") == wire


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("scenario", ["ordinary", "review", "failed_continue", "compaction", "child"])
async def test_released_response_contract(tmp_path, scenario, stream):
    executed = []

    @dagent.tool(risk="medium" if scenario == "review" else "low")
    def echo() -> str:
        executed.append("echo")
        return "evidence"

    initial = None
    policy = dagent.ContextPolicy()
    responses = [ChatResponse(tool_calls=[ToolCall(id="call", name="tool_echo", arguments={})]),
                 ChatResponse(content="done")]
    if scenario == "failed_continue":
        responses = [ChatResponse(), ChatResponse(content="continued")]
    if scenario == "compaction":
        initial = dagent.ConversationState(items=tuple(
            item for index in range(4) for item in (
                dagent.UserMessage(run_id=f"old_{index}", content="question " * 200),
                dagent.AssistantMessage(run_id=f"old_{index}", content="evidence " * 300),
            )
        ))
        responses.insert(0, ChatResponse(content="bounded summary"))
        policy = dagent.ContextPolicy(compaction_trigger_ratio=.4, summary_max_tokens=128)
    if scenario == "child":
        responses = [ChatResponse(tool_calls=[ToolCall(id="delegate", name="agent_helper", arguments={"prompt": "help"})]),
                     ChatResponse(content="child answer"), ChatResponse(content="parent answer")]
    provider = MockProvider(responses)
    if scenario == "compaction":
        provider.configured_context_window_tokens = 4096
        provider.max_output_tokens = 512
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        agent = dagent.ToolAgent(profile="conversation", capabilities=[echo], context=policy,
                                 review="careful" if scenario == "review" else "fast")
        if scenario == "child":
            runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[]))
            agent = dagent.ToolAgent(profile="conversation", capabilities=[], agents=["agent.helper"])

        result, events = await invoke(runner, agent, stream, input="echo", conversation=initial,
                                      run_id="contract", workspace_path=tmp_path / "work")
        assert_public_result(runner, result, events)
        if scenario == "review":
            assert result.status == "awaiting_review"
            assert result.review is not None
            assert executed == []
            saved = dagent.RunCheckpoint.model_validate_json(result.checkpoint.model_dump_json())
            if stream:
                events = [event async for event in runner.resume_stream(result.review.approve(), checkpoint=saved)]
                result = events[-1].data.result
            else:
                result, events = await runner.resume(result.review.approve(), checkpoint=saved), []
            assert_public_result(runner, result, events)
        if scenario == "failed_continue":
            assert result.status == "failed"
            assert result.error.code == "empty_response"
            saved = dagent.ConversationState.model_validate_json(result.conversation.model_dump_json())
            result, events = await invoke(runner, agent, stream, input="continue", conversation=saved,
                                          run_id="continued", workspace_path=tmp_path / "work")
            assert_public_result(runner, result, events)
            assert [item.content for item in result.conversation.items if isinstance(item, dagent.UserMessage)] == ["echo", "continue"]

        assert result.status == "completed"
        assert result.output_text == {"child": "parent answer", "failed_continue": "continued"}.get(scenario, "done")
        assert executed == ([] if scenario in {"child", "failed_continue"} else ["echo"])
        if scenario == "compaction":
            assert result.conversation.summary is not None
        if scenario not in {"child", "failed_continue"}:
            tool_results = [item for item in result.conversation.items if isinstance(item, dagent.ToolResultMessage)]
            assert len(tool_results) == 1
            assert tool_results[0].call_id == "call"
            assert tool_results[0].status == "completed"
            assert tool_results[0].content.text == "evidence"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("strategy", ["legacy", "chunked", "cache_aware"])
async def test_released_long_context_contract(tmp_path, strategy, stream):
    turns = summaries = 0
    executed = []
    request_streams = []

    @dagent.tool
    def query(index: int) -> str:
        executed.append(index)
        return f"page {index}: " + "evidence " * 100

    def respond(request):
        nonlocal turns, summaries
        body = json.loads(request.content)
        text = str(body["messages"])
        if "Sources to summarize:" in text or body["messages"][0]["content"].startswith("Produce a task checkpoint"):
            summaries += 1
            aliases = request_aliases(text)
            content = checkpoint(requirements=[("Collect pages.", [aliases[0]])],
                                 next_step=[("Continue.", [aliases[-1]])])
        elif body["messages"][0]["content"].startswith("Summarize earlier conversation data"):
            summaries += 1
            content = "Collected earlier pages; continue."
        else:
            turns += 1
            request_streams.append(body.get("stream", False))
            return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"),
                content="done" if turns > 32 else "",
                calls=() if turns > 32 else (ToolCall(id=f"call_{turns}", name="tool_query", arguments={"index": turns}),))
        return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"), content=content)

    async with _endpoint(protocol="chat_completions", token_counting="heuristic", context_window_tokens=4096,
                         max_output_tokens=512, responder=respond) as (provider, _):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            result, events = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[query],
                context=dagent.ContextPolicy(compaction_strategy=strategy, summary_max_tokens=512)), stream,
                input="Collect pages.", run_id="long", workspace_path=tmp_path / "work")
            assert_public_result(runner, result, events)
            assert result.status == "completed" and result.output_text == "done"
            assert summaries > 0 and result.conversation.summary is not None
            assert executed == list(range(1, 33))
            assert result.usage.capability_calls == 32
            assert result.usage.model_turns == turns + summaries
            assert request_streams == [stream] * turns
