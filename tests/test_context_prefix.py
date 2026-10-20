"""Compare histories in final HTTP payloads, not estimated cache hit rates."""

from contextlib import closing
import json

import pytest

import dagent
from dagent.harness_runtime.context import ContextAssembler
from dagent.providers import ToolCall
from dagent.schemas.conversation import inline_content
from tests.planner_helpers import final_answer_response
from tests.test_cache_usage import _completion, _usage
from tests.test_reasoning_mapping import _endpoint


def _history(body, protocol):
    return body["input" if protocol == "responses" else "messages"]


def _assert_fixed_fields(first, second, protocol):
    field = "input" if protocol == "responses" else "messages"
    assert {key: value for key, value in first.items() if key != field} == {
        key: value for key, value in second.items() if key != field
    }


def _assert_prefix(first, second, protocol):
    _assert_fixed_fields(first, second, protocol)
    history = _history(first, protocol)
    assert _history(second, protocol)[:len(history)] == history


def _without_reasoning(history, protocol):
    if protocol == "responses":
        return [item for item in history if item.get("type") != "reasoning"]
    return [{key: value for key, value in item.items() if key != "reasoning"} for item in history]


async def _run(runner, agent, stream, **arguments):
    if stream:
        events = [event async for event in runner.stream(agent, **arguments)]
        return events[-1].data.result
    return await runner.run(agent, **arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["active_run", "all_runs", "none"])
async def test_context_prefix_multiple_steps_and_continued_run(tmp_path, protocol, stream, mode):
    calls = 0

    @dagent.tool
    def echo(text: str) -> str:
        """Echo the input text."""
        return text

    def responder(request):
        nonlocal calls
        index = calls
        calls += 1
        return _completion(
            protocol, stream, usage=_usage(protocol), reasoning=f"trace {index}",
            content="" if index < 2 else "done",
            calls=(ToolCall(id=f"call_{index}", name="tool_echo", arguments={"text": str(index)}),)
            if index < 2 else (),
        )

    async with _endpoint(
        protocol=protocol, token_counting="heuristic", capture_cache_usage=True,
        responder=responder,
    ) as (provider, posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            agent = dagent.ToolAgent(profile="conversation", capabilities=[echo],
                                    context=dagent.ContextPolicy(reasoning_replay=mode))
            first = await _run(runner, agent, stream, input="first", workspace_path=tmp_path / "session")
            conversation = dagent.ConversationState.model_validate_json(first.conversation.model_dump_json())
            second = await _run(runner, agent, stream, input="next", conversation=conversation,
                                workspace_path=tmp_path / "session")
    assert first.status == second.status == "completed"
    bodies = [json.loads(post.content) for post in posts]
    assert len(bodies) == 4
    _assert_prefix(bodies[0], bodies[1], protocol)
    _assert_prefix(bodies[1], bodies[2], protocol)
    if mode == "active_run":
        _assert_fixed_fields(bodies[2], bodies[3], protocol)
        old = _history(bodies[2], protocol)
        projected = _without_reasoning(old, protocol)
        assert projected != old
        assert _history(bodies[3], protocol)[:len(projected)] == projected
    else:
        _assert_prefix(bodies[2], bodies[3], protocol)
    assert all(item.reasoning for item in conversation.items if isinstance(item, dagent.AssistantMessage))
    assert any(item.usage.cached_input_tokens == 80 for item in conversation.items
               if isinstance(item, dagent.AssistantMessage))
    assert all("cached_input_tokens" not in json.dumps(body) for body in bodies)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_context_prefix_review_resume_preserves_frozen_history(tmp_path, protocol):
    calls = 0
    executed = []

    @dagent.tool
    def inspect() -> str:
        """Inspect existing data."""
        executed.append("inspect")
        return "ready"

    @dagent.tool(risk="medium")
    def record() -> str:
        """Record the decision."""
        executed.append("record")
        return "recorded"

    def responder(request):
        nonlocal calls
        index = calls
        calls += 1
        return _completion(
            protocol, False, usage=_usage(protocol), reasoning=f"trace {index}",
            content="" if index < 2 else "done",
            calls=(ToolCall(id=f"call_{index}", name="tool_inspect" if index == 0 else "tool_record", arguments={}),)
            if index < 2 else (),
        )

    async with _endpoint(
        protocol=protocol, token_counting="heuristic", capture_cache_usage=True,
        responder=responder,
    ) as (provider, posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            first = await runner.run(dagent.ToolAgent(
                profile="conversation", capabilities=[inspect, record], review="careful",
            ), input="inspect then record", workspace_path=tmp_path / "session")
            assert first.status == "awaiting_review" and executed == ["inspect"]
            checkpoint = dagent.RunCheckpoint.model_validate_json(first.checkpoint.model_dump_json())
        with closing(dagent.Runner(
            provider=provider, workspace=tmp_path, skill_roots=[], capabilities=[inspect, record],
        )) as resumed:
            result = await resumed.resume(first.review.approve(), checkpoint=checkpoint)
    assert result.status == "completed" and executed == ["inspect", "record"]
    bodies = [json.loads(post.content) for post in posts]
    assert len(bodies) == 3
    _assert_prefix(bodies[1], bodies[2], protocol)
    history = _history(bodies[2], protocol)
    if protocol == "responses":
        assert [item["call_id"] for item in history if item.get("type") == "function_call"] == ["call_0", "call_1"]
        assert [item["call_id"] for item in history if item.get("type") == "function_call_output"] == ["call_0", "call_1"]
    else:
        assert [item["tool_calls"][0]["id"] for item in history if item.get("tool_calls")] == ["call_0", "call_1"]
        assert [item["tool_call_id"] for item in history if item["role"] == "tool"] == ["call_0", "call_1"]
    assert all(item.usage.cached_input_tokens == 80 for item in checkpoint.state.model_thread.items
               if isinstance(item, dagent.AssistantMessage))


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_context_prefix_compaction_explicitly_replaces_old_history(tmp_path, protocol):
    calls = 0

    def responder(request):
        nonlocal calls
        calls += 1
        return _completion(protocol, False, usage=_usage(protocol),
                           content="bounded summary" if calls == 2 else "done")

    async with _endpoint(
        protocol=protocol, token_counting="heuristic", context_window_tokens=2048,
        max_output_tokens=256, responder=responder,
    ) as (provider, posts):
        agent = dagent.ToolAgent(profile="conversation", capabilities=[], context=dagent.ContextPolicy(
            compaction_trigger_ratio=0.2, summary_max_tokens=64,
        ))
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            first = await runner.run(agent, input="x" * 600, workspace_path=tmp_path / "session")
            second = await runner.run(agent, input="y" * 600, conversation=first.conversation,
                                      workspace_path=tmp_path / "session")
    assert first.status == second.status == "completed" and calls == 3
    bodies = [json.loads(post.content) for post in posts]
    assert second.conversation.summary.content == "bounded summary"
    _assert_fixed_fields(bodies[0], bodies[2], protocol)
    history = _history(bodies[2], protocol)
    user_items = [item for item in history if item.get("role") == "user"]
    assert "[Earlier conversation summary" in user_items[0]["content"]
    assert user_items[1]["content"] == "y" * 600
    assert "x" * 600 not in json.dumps(history)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_context_prefix_reasoning_pressure_only_changes_projection(protocol):
    conversation = dagent.ConversationState(items=(
        dagent.UserMessage(run_id="run", content="request"),
        dagent.AssistantMessage(
            run_id="run", content="using tool", reasoning="old reasoning " * 100,
            tool_calls=(dagent.ToolCallItem(id="call", name="lookup"),),
        ),
        dagent.ToolResultMessage(run_id="run", call_id="call", name="lookup", status="completed",
                                 content=inline_content("ok")),
        dagent.AssistantMessage(run_id="run", content="latest step", reasoning="latest reasoning " * 20),
    ))
    async with _endpoint(protocol=protocol, token_counting="heuristic") as (provider, posts):
        prepared = []
        for window in (8192, 1024):
            context = await ContextAssembler(
                context_window_tokens=window, max_output_tokens=128,
                request_reasoning_field=provider.context_reasoning_field,
            ).prepare(
                system_message={"content": "Be useful."}, conversation=conversation,
                policy=dagent.ContextPolicy(compaction_trigger_ratio=0.4), active_run_id="run",
            )
            prepared.append(context)
            await provider.complete(context.request)
    assert prepared[1].request.reasoning_omission_reasons == ("context_budget",)
    assert prepared[0].conversation == prepared[1].conversation == conversation
    assert prepared[1].usage.compacted_items == 0
    bodies = [json.loads(post.content) for post in posts]
    _assert_fixed_fields(bodies[0], bodies[1], protocol)
    assert "old reasoning" in json.dumps(_history(bodies[0], protocol))
    assert "old reasoning" not in json.dumps(_history(bodies[1], protocol))
    assert "latest reasoning" in json.dumps(_history(bodies[1], protocol))


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_context_prefix_dag_continuation_rebuilds_public_history(tmp_path, protocol):
    async with _endpoint(
        protocol=protocol, token_counting="heuristic",
        responder=lambda request: _completion(protocol, False, usage=_usage(protocol),
                                               content=final_answer_response("done")),
    ) as (provider, posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            agent = dagent.DagAgent(capabilities=[])
            first = await runner.run(agent, input="first", workspace_path=tmp_path / "session")
            second = await runner.run(agent, input="next", conversation=first.conversation,
                                      workspace_path=tmp_path / "session")
    assert first.status == second.status == "completed"
    bodies = [json.loads(post.content) for post in posts]
    _assert_fixed_fields(bodies[0], bodies[1], protocol)
    before = [item for item in _history(bodies[0], protocol) if item.get("role") == "user"]
    after = [item for item in _history(bodies[1], protocol) if item.get("role") == "user"]
    assert before[0]["content"] == f"Task id: {first.run_id}\nfirst"
    assert after[0]["content"] == "first"
