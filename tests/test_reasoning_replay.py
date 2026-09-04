from __future__ import annotations

import pytest

from dagent.harness_runtime.context import ContextAssembler
from dagent.providers.model_io import ModelAssistantTurn, ModelTokenCount
from dagent.schemas import (
    AssistantMessage,
    ContextPolicy,
    ConversationState,
    ToolCallItem,
    ToolResultMessage,
    UserMessage,
)
from dagent.schemas.conversation import inline_content


def test_context_policy_rejects_removed_turn_count_setting() -> None:
    with pytest.raises(ValueError, match="keep_recent_turns"):
        ContextPolicy.model_validate({"keep_recent_turns": 4})


def _conversation() -> ConversationState:
    return ConversationState(
        items=(
            UserMessage(run_id="run_1", content="first request"),
            AssistantMessage(
                run_id="run_1",
                content="first answer",
                reasoning="reasoning from run one",
            ),
            UserMessage(run_id="run_2", content="second request"),
            AssistantMessage(
                run_id="run_2",
                content="I will inspect it.",
                reasoning="reasoning from run two",
                tool_calls=(
                    ToolCallItem(id="call_2", name="lookup", arguments={}),
                ),
            ),
            ToolResultMessage(
                run_id="run_2",
                call_id="call_2",
                name="lookup",
                status="completed",
                content=inline_content("ok"),
            ),
        )
    )


def _reasoning(request) -> list[str]:
    return [
        item.reasoning
        for item in request.items
        if isinstance(item, ModelAssistantTurn)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("none", ["", ""]),
        ("active_run", ["", "reasoning from run two"]),
        ("all_runs", ["reasoning from run one", "reasoning from run two"]),
    ],
)
async def test_reasoning_replay_modes(mode: str, expected: list[str]) -> None:
    conversation = _conversation()
    prepared = await ContextAssembler(context_window_tokens=8192).prepare(
        system_message={"role": "system", "content": "Be useful."},
        conversation=conversation,
        policy=ContextPolicy(reasoning_replay=mode),  # type: ignore[arg-type]
        active_run_id="run_2",
    )

    assert _reasoning(prepared.request) == expected
    assert prepared.usage.reasoning_replay_mode == mode
    assert prepared.usage.replayed_reasoning_items == sum(bool(x) for x in expected)
    assert prepared.conversation == conversation


@pytest.mark.asyncio
async def test_pressure_drops_oldest_reasoning_but_keeps_latest_step() -> None:
    first_reasoning = "old reasoning " * 100
    latest_reasoning = "latest reasoning " * 20
    conversation = ConversationState(
        items=(
            UserMessage(run_id="run_1", content="request"),
            AssistantMessage(
                run_id="run_1",
                content="using tool",
                reasoning=first_reasoning,
                tool_calls=(
                    ToolCallItem(id="call_1", name="lookup", arguments={}),
                ),
            ),
            ToolResultMessage(
                run_id="run_1",
                call_id="call_1",
                name="lookup",
                status="completed",
                content=inline_content("ok"),
            ),
            AssistantMessage(
                run_id="run_1",
                content="latest step",
                reasoning=latest_reasoning,
            ),
        )
    )

    prepared = await ContextAssembler(
        context_window_tokens=1024,
        output_reserve_tokens=128,
    ).prepare(
        system_message={"role": "system", "content": "Be useful."},
        conversation=conversation,
        policy=ContextPolicy(
            reasoning_replay="active_run",
            compaction_trigger_ratio=0.4,
        ),
        active_run_id="run_1",
    )

    assert _reasoning(prepared.request) == ["", latest_reasoning]
    assert prepared.usage.omitted_reasoning_items == 1
    assert prepared.usage.replayed_reasoning_items == 1
    original = next(
        item
        for item in prepared.conversation.items
        if isinstance(item, AssistantMessage) and item.id == conversation.items[1].id
    )
    assert original.reasoning == first_reasoning


@pytest.mark.asyncio
async def test_exact_counter_supplies_server_window_and_observability() -> None:
    async def count(_request):
        return ModelTokenCount(count=100, max_model_len=65536, estimator="vllm")

    prepared = await ContextAssembler(
        output_reserve_tokens=4096,
        request_token_counter=count,
    ).prepare(
        system_message={"role": "system", "content": "Be useful."},
        conversation=ConversationState(items=(UserMessage(content="hello"),)),
        policy=ContextPolicy(token_safety_margin=0),
    )

    assert prepared.usage.estimator == "vllm"
    assert prepared.usage.estimated_input_tokens == 100
    assert prepared.usage.server_max_model_len == 65536
    assert prepared.usage.context_window_tokens == 65536
    assert prepared.usage.configured_context_limit is None
