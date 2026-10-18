from __future__ import annotations

import json

import pytest

from dagent.harness_runtime.context import ContextAssembler
from dagent.harness_runtime.result_storage import ResultStore
from dagent.schemas import (
    AssistantMessage,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("observations", [False, True])
@pytest.mark.parametrize("window", [4096, 131072])
async def test_appending_results_only_changes_history_at_context_compaction(tmp_path, observations, window):
    store = ResultStore(tmp_path, ".runtime")
    policy = ContextPolicy(max_tool_result_tokens=1024, max_total_tool_result_tokens=2048)
    conversation = ConversationState(items=(UserMessage(run_id="run", content="Inspect data"),))
    previous_items = {}
    previous_messages = None
    compacted = []

    async def summarize(previous, items, limit):
        compacted.append(items)
        return ContextSummary(
            content="Earlier results inspected.",
            source_item_count=(previous.source_item_count if previous else 0) + len(items),
        )

    for index in range(5):
        original = f"result {index}:\n" + "数据" * 5000
        if observations:
            new_items = (UserMessage(
                id=f"observation-{index}", run_id="run", content="Inspect result",
                result_observations=(ResultObservation(
                    id=f"result-{index}", name="query", status="completed",
                    content=inline_content(original),
                ),),
            ),)
        else:
            new_items = (
                AssistantMessage(
                    run_id="run", tool_calls=(ToolCallItem(id=f"call-{index}", name="query"),),
                ),
                ToolResultMessage(
                    id=f"result-{index}", run_id="run", call_id=f"call-{index}",
                    name="query", status="completed", content=inline_content(original),
                ),
            )
        conversation = conversation.model_copy(update={"items": (*conversation.items, *new_items)})
        # A new assembler and JSON continuation exercise persisted history, not a
        # process-local cache that could change after review or run continuation.
        prepared = await ContextAssembler(context_window_tokens=window).prepare(
            system_message={"content": "Inspect data"}, conversation=conversation,
            policy=policy, tools=[READ_TOOL], result_store=store, compact=summarize,
            active_run_id="run",
        )
        current_items = {item.source_id: item for item in prepared.request.items
                         if item.source_id != "context_summary"}
        for identity in previous_items.keys() & current_items.keys():
            assert current_items[identity] == previous_items[identity]
        if prepared.usage.compacted_items == 0:
            if previous_messages is not None:
                assert prepared.messages[:len(previous_messages)] == previous_messages
        else:
            assert prepared.usage.compaction_method == "model"
            if not observations:
                for assistant, result in zip(compacted[-1][::2], compacted[-1][1::2]):
                    assert assistant.tool_calls[0].id == result.call_id
        retained_results = sum(
            len(item.result_observations) if isinstance(item, UserMessage)
            else isinstance(item, ToolResultMessage)
            for item in prepared.conversation.items
        )
        assert prepared.usage.tool_result_tokens == retained_results * 1024
        latest = prepared.conversation.items[-1]
        result = latest.result_observations[0] if observations else latest
        assert (tmp_path / result.content.path).read_text() == original
        previous_items = current_items
        previous_messages = prepared.messages
        conversation = ConversationState.model_validate_json(prepared.conversation.model_dump_json())

    if window == 131072:
        assert not compacted
        assert conversation.summary is None
        assert prepared.usage.tool_result_tokens == 5120
    else:
        assert compacted
        rows = [json.loads(line) for line in (
            tmp_path / conversation.summary.result_manifest.path
        ).read_text().splitlines()]
        archived = [row for row in rows if row["type"] in {"result_observation", "tool_result"}]
        expected = [result.id for group in compacted for item in group
                    for result in (item.result_observations if observations
                                   else (item,) if isinstance(item, ToolResultMessage) else ())]
        assert [row["id"] for row in archived] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("observations", [False, True])
async def test_hard_window_overflow_does_not_refit_result_display(tmp_path, observations):
    original = "数据" * 20000
    result = (
        ResultObservation(id="result", name="query", status="completed", content=inline_content(original))
        if observations else ToolResultMessage(
            call_id="call", name="query", status="completed", content=inline_content(original),
        )
    )
    conversation = ConversationState(items=(
        (UserMessage(content="Inspect", result_observations=(result,)),)
        if observations else (
            UserMessage(content="Inspect"),
            AssistantMessage(tool_calls=(ToolCallItem(id="call", name="query"),)),
            result,
        )
    ))
    policy = ContextPolicy(max_tool_result_tokens=32768, max_total_tool_result_tokens=65536)
    store = ResultStore(tmp_path, ".runtime")
    prepared = await ContextAssembler(context_window_tokens=131072).prepare(
        system_message={"content": "Inspect"}, conversation=conversation, tools=[READ_TOOL],
        policy=policy, result_store=store,
    )
    with pytest.raises(ContextWindowExceeded) as caught:
        await ContextAssembler(context_window_tokens=4096).prepare(
            system_message={"content": "Inspect"}, conversation=prepared.conversation,
            tools=[READ_TOOL], policy=policy, result_store=store,
        )
    assert caught.value.usage.tool_result_tokens == prepared.usage.tool_result_tokens
    assert caught.value.usage.compacted_items == 0
    assert caught.value.usage.estimated_input_tokens > caught.value.usage.input_budget_tokens


@pytest.mark.asyncio
@pytest.mark.parametrize("total_budget", [64, 2048])
async def test_parallel_results_do_not_share_the_deprecated_total_budget(total_budget):
    conversation = ConversationState(items=(
        AssistantMessage(tool_calls=tuple(ToolCallItem(id=str(i), name="query") for i in range(3))),
        *(ToolResultMessage(call_id=str(i), name="query", status="completed",
                            content=inline_content("数据" * 3000)) for i in range(3)),
    ))
    policy = ContextPolicy.model_validate_json(ContextPolicy(
        max_tool_result_tokens=1024, max_total_tool_result_tokens=total_budget,
    ).model_dump_json())
    prepared = await ContextAssembler().prepare(
        system_message={"content": "Inspect"}, conversation=conversation, policy=policy,
    )
    assert prepared.usage.tool_result_tokens == 3072
    assert prepared.usage.compacted_items == 0
    assert prepared.conversation == conversation
