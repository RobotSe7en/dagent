from __future__ import annotations

import math
import json
from contextlib import closing

import pytest

import dagent
from dagent.harness_runtime.context import ContextAssembler, HeuristicTokenCounter
from dagent.harness_runtime.result_storage import ResultStore
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.providers.model_io import ModelTokenCount, model_request_to_chat
from dagent.schemas import (
    AssistantMessage,
    ContentReference,
    ContextPolicy,
    ContextSummary,
    ContextWindowExceeded,
    ConversationState,
    ToolCallItem,
    ToolResultMessage,
    UserMessage,
)
from dagent.schemas.conversation import ResultObservation, inline_content


READ_TOOL = {"type": "function", "function": {"name": "tool_read_file"}}


def tool_history(*, count: int, text: str, old_steps: bool = True) -> ConversationState:
    return ConversationState(items=(
        UserMessage(run_id="run", content="Inspect the data."),
        *((AssistantMessage(run_id="run", content="earlier work " * 20000),) if old_steps else ()),
        AssistantMessage(
            run_id="run",
            tool_calls=tuple(ToolCallItem(id=f"call_{i}", name="query") for i in range(count)),
        ),
        *(ToolResultMessage(
            run_id="run", call_id=f"call_{i}", name="query", status="completed",
            content=inline_content(text), value={"index": i},
        ) for i in range(count)),
    ))


async def summarize(previous, items, limit):
    return ContextSummary(
        content="Earlier checks completed.",
        source_item_count=(previous.source_item_count if previous else 0) + len(items),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("window,count", [(32768, 1), (65536, 2), (131072, 4)])
@pytest.mark.parametrize("exact", [False, True])
async def test_large_latest_results_fit_after_history_compaction(window, count, exact):
    counter = HeuristicTokenCounter()
    token_requests = []

    async def count_tokens(request, reasoning_field):
        token_requests.append(request)
        # Simulate a tokenizer whose counts exceed the local text heuristic.
        return ModelTokenCount(
            count=math.ceil(counter.count_request(
                model_request_to_chat(request, reasoning_field=reasoning_field), request.tools,
            ) * 1.3),
            max_model_len=window,
        )

    policy = ContextPolicy(max_tool_result_tokens=32768, max_total_tool_result_tokens=131072)
    conversation = tool_history(count=count, text="数据" * 30000)
    assembler = ContextAssembler(
        context_window_tokens=window, max_output_tokens=1024,
        request_token_counter=count_tokens if exact else None,
    )
    prepared = await assembler.prepare(
        system_message={"content": "Inspect the data."}, conversation=conversation,
        policy=policy, compact=summarize, active_run_id="run",
    )

    assert prepared.usage.compaction_method == "model"
    assert prepared.usage.compacted_active_run_items == 1
    assert prepared.usage.estimated_input_tokens <= prepared.usage.compaction_trigger_tokens
    assert prepared.usage.input_budget_tokens == window - 1024
    assert prepared.usage.truncated_tool_results == count
    assert prepared.conversation.items == (conversation.items[0], *conversation.items[2:])
    tool_messages = [message for message in prepared.messages if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == [f"call_{i}" for i in range(count)]
    assert all("[status=completed]" in message["content"] for message in tool_messages)
    assert all("[TRUNCATED]" in message["content"] for message in tool_messages)
    assert all("数据" in message["content"] for message in tool_messages)
    assert policy.max_total_tool_result_tokens == 131072
    if exact:
        assert prepared.usage.estimator == "vllm"
        assert prepared.usage.estimated_input_tokens == (await count_tokens(prepared.request, "omit")).count
        assert len(token_requests) < 30
    else:
        assert prepared.usage.estimated_input_tokens == math.ceil(
            counter.count_request(prepared.messages, prepared.request.tools) * 1.15
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("observations", [False, True])
async def test_newly_shortened_results_are_saved_and_reused(tmp_path, observations):
    original = "原始数据 abc " * 2500
    policy = ContextPolicy(max_tool_result_tokens=32768, max_total_tool_result_tokens=131072)
    conversation = (
        ConversationState(items=(UserMessage(
            run_id="run", content="Inspect observations", result_observations=(ResultObservation(
                id="result", name="query", status="completed", content=inline_content(original),
            ),),
        ),))
        if observations else tool_history(count=1, text=original, old_steps=False)
    )
    assembler = ContextAssembler(context_window_tokens=4096, max_output_tokens=512)
    store = ResultStore(tmp_path, ".runtime")
    prepared = await assembler.prepare(
        system_message={"content": "Inspect data"}, conversation=conversation,
        tools=[READ_TOOL], policy=policy, result_store=store,
    )
    result = (prepared.conversation.items[0].result_observations[0] if observations
              else prepared.conversation.items[-1])
    assert isinstance(result.content, ContentReference)
    assert (tmp_path / result.content.path).read_text() == original
    assert prepared.usage.estimated_input_tokens <= prepared.usage.compaction_trigger_tokens
    assert prepared.usage.compaction_method == "none"
    assert prepared.usage.truncated_tool_results == 1
    assert prepared.usage.unrecoverable_tool_results == 0
    assert original in str(conversation.model_dump())
    files = set(store.root.iterdir())
    again = await assembler.prepare(
        system_message={"content": "Inspect data"}, conversation=prepared.conversation,
        tools=[READ_TOOL], policy=policy, result_store=store,
    )
    assert again.conversation == prepared.conversation
    assert set(store.root.iterdir()) == files
    assert again.usage.estimated_input_tokens <= again.usage.input_budget_tokens


@pytest.mark.asyncio
async def test_soft_pressure_alone_does_not_shorten_latest_result(tmp_path):
    original = "x" * 2000
    conversation = tool_history(count=1, text=original, old_steps=False)
    prepared = await ContextAssembler(context_window_tokens=2048).prepare(
        system_message={"content": "Inspect data"}, conversation=conversation,
        policy=ContextPolicy(compaction_trigger_ratio=0.2),
        result_store=ResultStore(tmp_path, ".runtime"),
    )
    assert prepared.usage.compaction_trigger_tokens < prepared.usage.estimated_input_tokens
    assert prepared.usage.estimated_input_tokens < prepared.usage.input_budget_tokens
    assert prepared.usage.truncated_tool_results == 0
    assert prepared.conversation == conversation
    assert original in prepared.messages[-1]["content"]


@pytest.mark.asyncio
async def test_minimum_result_above_soft_target_still_fits_hard_budget():
    prepared = await ContextAssembler(context_window_tokens=1024, max_output_tokens=128).prepare(
        system_message={"content": "x" * 2100},
        conversation=tool_history(count=1, text="数据" * 3000, old_steps=False),
        policy=ContextPolicy(compaction_trigger_ratio=0.5),
    )
    assert prepared.usage.compaction_trigger_tokens < prepared.usage.estimated_input_tokens
    assert prepared.usage.estimated_input_tokens <= prepared.usage.input_budget_tokens
    assert prepared.usage.tool_result_body_tokens > 0
    assert "[TRUNCATED]" in prepared.messages[-1]["content"]


@pytest.mark.asyncio
async def test_mandatory_input_still_fails_before_generation(tmp_path):
    provider = MockProvider([ChatResponse(content="must not be called")])
    provider.context_window_tokens = 2048
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        with pytest.raises(ContextWindowExceeded) as caught:
            await runner.run(
                dagent.ToolAgent(profile="conversation", capabilities=[]),
                input="必须保留" * 2000,
            )
    assert caught.value.usage.estimated_input_tokens > caught.value.usage.input_budget_tokens
    assert provider.requests == []


@pytest.mark.asyncio
async def test_latest_reasoning_is_preserved_when_results_cannot_fit():
    async def reasoning_field(request, stream):
        return "reasoning"

    conversation = tool_history(count=1, text="数据" * 3000, old_steps=False)
    latest = conversation.items[1].model_copy(update={"reasoning": "当前推理" * 1000})
    conversation = conversation.model_copy(update={"items": (
        conversation.items[0], latest, conversation.items[2],
    )})
    with pytest.raises(ContextWindowExceeded) as caught:
        await ContextAssembler(
            context_window_tokens=2048, request_reasoning_field=reasoning_field,
        ).prepare(system_message={"content": "Inspect"}, conversation=conversation, policy=ContextPolicy())
    assert caught.value.usage.replayed_reasoning_items == 1
    assert caught.value.usage.omitted_reasoning_items == 0
    assert caught.value.usage.tool_result_body_tokens > 0
    assert caught.value.usage.tool_result_tokens < 2048


@pytest.mark.asyncio
async def test_result_storage_alone_does_not_emit_compaction_finished(tmp_path):
    conversation = tool_history(count=1, text="x" * 20000, old_steps=False)
    with closing(dagent.Runner(provider=MockProvider([ChatResponse(content="ok")]), workspace=tmp_path)) as runner:
        events = [event async for event in runner.stream(
            dagent.ToolAgent(profile="conversation", capabilities=[]),
            input="Continue", conversation=conversation,
        )]
    assert not any(event.type.startswith("context.compaction.") for event in events)
    result = events[-1].data.result
    assert result.conversation.summary is None
    assert result.context_usage[-1].truncated_tool_results == 1


@pytest.mark.asyncio
async def test_dynamic_shortening_preserves_storage_warning(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    store = ResultStore(tmp_path, ".runtime")
    monkeypatch.setattr(store, "save_text", fail)
    original = "数据" * 3000
    with pytest.warns(RuntimeWarning, match="disk unavailable"):
        prepared = await ContextAssembler(context_window_tokens=2048).prepare(
            system_message={"content": "Inspect"},
            conversation=tool_history(count=1, text=original, old_steps=False),
            policy=ContextPolicy(max_tool_result_tokens=32768, max_total_tool_result_tokens=131072),
            result_store=store,
        )
    assert prepared.conversation.items[-1].content.text == original
    assert prepared.conversation.items[-1].retention.storage_warnings
    assert prepared.usage.unrecoverable_tool_results == 1
    assert prepared.usage.estimated_input_tokens <= prepared.usage.compaction_trigger_tokens


@pytest.mark.asyncio
async def test_dynamic_file_excerpt_keeps_correct_continuation(tmp_path):
    from dagent.capabilities.tools.file_tools import read_file
    from dagent.schemas.retention import ResultRetention

    original = "中文🙂abcdef" * 1500
    (tmp_path / "report.txt").write_text(original)
    output = read_file(tmp_path / "report.txt", offset_chars=0, limit_chars=len(original))
    conversation = tool_history(count=1, text=output.content, old_steps=False)
    result = conversation.items[-1].model_copy(update={
        "retention": ResultRetention.model_validate(output.retention),
    })
    conversation = conversation.model_copy(update={"items": (*conversation.items[:-1], result)})
    store = ResultStore(tmp_path, ".runtime")
    prepared = await ContextAssembler(context_window_tokens=2048).prepare(
        system_message={"content": "Inspect"}, conversation=conversation,
        policy=ContextPolicy(max_tool_result_tokens=32768, max_total_tool_result_tokens=131072),
        tools=[READ_TOOL], result_store=store,
    )
    content = prepared.messages[-1]["content"]
    cursor = next(line for line in content.splitlines() if line.startswith("[Continue with read_file:"))
    args = json.loads(cursor[len("[Continue with read_file: "):-1])
    body = content.split(cursor + "\n", 1)[1]
    assert original[:args["offset_chars"]] == body
    assert 0 < args["offset_chars"] < len(original)
    assert prepared.conversation.items[-1].retention == result.retention
    assert prepared.usage.estimated_input_tokens <= prepared.usage.compaction_trigger_tokens
    assert not store.root.exists()


@pytest.mark.asyncio
async def test_review_resume_uses_frozen_policy_and_preserves_large_result(tmp_path):
    executions = []
    original = "完整结果" * 6000

    @dagent.tool(risk="medium")
    def report() -> str:
        executions.append("report")
        return original

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="report", name="tool_report", arguments={})]),
        ChatResponse(content="done"),
    ])
    provider.context_window_tokens = 8192
    provider.max_output_tokens = 1024
    policy = ContextPolicy(max_tool_result_tokens=32768, max_total_tool_result_tokens=131072)
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        pending = await runner.run(dagent.ToolAgent(
            profile="conversation", capabilities=[report], context=policy, review="careful",
        ), input="Make the report", workspace_path=tmp_path)
        assert pending.requires_review
        checkpoint = dagent.RunCheckpoint.model_validate_json(pending.checkpoint.model_dump_json())
        assert executions == []
    provider.context_window_tokens = 16384
    provider.max_output_tokens = 2048
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, capabilities=[report], skill_roots=[])) as runner:
        result = await runner.resume(pending.review.approve(), checkpoint=checkpoint)
    assert result.status == "completed"
    assert executions == ["report"]
    usage = result.context_usage[-1]
    assert usage.context_window_tokens == 8192
    assert usage.max_output_tokens == 1024
    assert usage.estimated_input_tokens <= usage.compaction_trigger_tokens
    assert result.checkpoint.plan.context_policy == policy
    stored = next(item for item in result.conversation.items if isinstance(item, ToolResultMessage))
    assert isinstance(stored.content, ContentReference)
    assert (tmp_path / stored.content.path).read_text() == original
    assert "[TRUNCATED]" in next(
        message["content"] for message in provider.requests[-1]["messages"] if message["role"] == "tool"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_kind", ["tool", "dag", "profiled", "subagent"])
async def test_new_agent_inherits_verified_window_when_counter_unavailable(tmp_path, agent_kind):
    from dagent.harness_runtime.profiled_agent import ProfiledAgent
    from dagent.profiles import AgentProfile

    provider = MockProvider([])
    provider.configured_context_window_tokens = None
    provider.server_max_model_len = 32768
    provider.context_window_tokens = 32768
    oversized = "x" * 200000
    if agent_kind == "profiled":
        agent = ProfiledAgent(provider=provider, profile=AgentProfile(name="test", content="Be useful"))
        with pytest.raises(ContextWindowExceeded) as caught:
            await agent.run_text(task_content=oversized)
    else:
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            if agent_kind == "subagent":
                dag = dagent.Dag("check")
                dag.add_node(dagent.Node("helper", target=dagent.ToolAgent(
                    name="helper", profile="conversation", capabilities=[], skills=[],
                ), inputs={"prompt": oversized}))
                result = await runner.run(dag)
                assert result.status == "failed"
                assert provider.requests == []
                assert "32767 input tokens" in str(result.trace.model_dump())
                return
            agent = (dagent.ToolAgent(profile="conversation", capabilities=[]) if agent_kind == "tool"
                     else dagent.DagAgent(capabilities=[]))
            with pytest.raises(ContextWindowExceeded) as caught:
                await runner.run(agent, input=oversized)
    assert caught.value.usage.context_window_tokens == 32768
    assert caught.value.usage.context_window_source == "server"
    assert caught.value.usage.estimator == "heuristic"
    assert provider.requests == []
