"""Incremental submissions preserve the released host batch stream."""

import asyncio
from contextlib import closing
from itertools import count
import sys
from uuid import UUID

import pytest

import dagent
from dagent.harness_runtime import tool_agent
from dagent.providers import ChatResponse, MockProvider, ToolCall
from tests.test_conversation_changes import Host, invoke, resume_invoke
from tests.test_steering import GatedProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_ordinary_steps_submit_only_new_messages(tmp_path, monkeypatch, stream):
    @dagent.tool
    def echo() -> str:
        return "ok"

    steps = 40
    provider = MockProvider([
        *(ChatResponse(tool_calls=[ToolCall(id=f"call_{index}", name="tool_echo", arguments={})])
          for index in range(steps)),
        ChatResponse(content="done"),
    ])
    submitted = []
    original = tool_agent.publish_state

    async def observe(state, items=(), observations=()):
        submitted.append(tuple(items))
        await original(state, items, observations)

    monkeypatch.setattr(tool_agent, "publish_state", observe)
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        result, _ = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[echo]), host, stream, input="repeat")
    assert host.state == result.conversation
    assert max(map(len, submitted)) == 1
    assert sum(map(len, submitted)) == steps
    # Initial state + one preparation per reply + one publication per result.
    # Empty steer drains must not add another publication per model step.
    assert len(submitted) == 2 * steps + 2


@pytest.mark.asyncio
async def test_steer_submits_only_its_new_message(tmp_path, monkeypatch):
    @dagent.tool
    def echo() -> str:
        return "ok"

    provider = GatedProvider([
        *(ChatResponse(tool_calls=[ToolCall(id=f"call_{index}", name="tool_echo", arguments={})])
          for index in range(10)),
        ChatResponse(content="stale"), ChatResponse(content="redirected"),
    ], blocked={10})
    submitted = []
    original = tool_agent.publish_state

    async def observe(state, items=(), observations=()):
        submitted.append(tuple(items))
        await original(state, items, observations)

    monkeypatch.setattr(tool_agent, "publish_state", observe)
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        task = asyncio.create_task(invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[echo]),
                                         host, False, input="repeat", run_id="steered"))
        await asyncio.wait_for(provider.started[10].wait(), 5)
        receipt = await runner.steer("steered", "new direction")
        provider.release[10].set()
        result, _ = await asyncio.wait_for(task, 5)
    steer_batches = [items for items in submitted if any(item.id == receipt.steer_id for item in items)]
    assert len(steer_batches) == 1 and len(steer_batches[0]) == 1
    assert host.state == result.conversation


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("scenario", ["plain", "overflow", "compaction", "review", "nested"])
async def test_incremental_and_cumulative_submissions_deliver_identical_batches(tmp_path, monkeypatch, stream, scenario):
    original = tool_agent.publish_state
    streams = []
    for cumulative in (True, False):
        with monkeypatch.context() as patch:
            # Stable identities also keep archive digests and response links
            # comparable. Timings and trace timestamps are not in the batches.
            identities = count(1)
            for name, module in tuple(sys.modules.items()):
                if name.startswith("dagent.") and hasattr(module, "uuid4"):
                    patch.setattr(module, "uuid4", lambda: UUID(int=next(identities)))
            if cumulative:
                initial_ids = {}

                async def publish_cumulative(state, items=(), observations=()):
                    initial = initial_ids.setdefault(state.id, {item.id for item in state.items})
                    await original(state, tuple(item for item in state.items if item.id not in initial), observations)

                patch.setattr(tool_agent, "publish_state", publish_cumulative)

            @dagent.tool(risk="medium" if scenario == "review" else "low")
            def echo() -> str:
                return "evidence " * 200 if scenario == "overflow" else "ok"

            responses = [
                ChatResponse(tool_calls=[ToolCall(id="call", name="tool_echo", arguments={})]),
                ChatResponse(content="done"),
            ]
            initial = None
            capabilities = [echo]
            policy = dagent.ContextPolicy(max_tool_result_tokens=64)
            if scenario == "overflow":
                capabilities.append("tool.read_file")
            if scenario == "compaction":
                initial = dagent.ConversationState(revision=8, items=tuple(
                    item for index in range(4) for item in (
                        dagent.UserMessage(run_id=f"old_{index}", content="question " * 200),
                        dagent.AssistantMessage(run_id=f"old_{index}", content="evidence " * 300),
                    )
                ))
                responses.insert(0, ChatResponse(content="bounded summary"))
                policy = dagent.ContextPolicy(compaction_trigger_ratio=.4, summary_max_tokens=128)
            if scenario == "nested":
                responses = [
                    ChatResponse(tool_calls=[ToolCall(id="outer", name="agent_helper", arguments={"task": "echo"})]),
                    *responses, ChatResponse(content="outer done"),
                ]
            provider = MockProvider(responses)
            if scenario == "compaction":
                provider.configured_context_window_tokens = 4096
                provider.max_output_tokens = 512
            with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
                agent = dagent.ToolAgent(profile="conversation", capabilities=capabilities, context=policy,
                                         review="careful" if scenario == "review" else "fast")
                if scenario == "nested":
                    runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=capabilities, context=policy))
                    agent = dagent.ToolAgent(profile="conversation", capabilities=[], agents=["agent.helper"])
                host = Host(initial, runner)
                result, _ = await invoke(runner, agent, host, stream, input="echo", conversation=initial,
                                         run_id="publication", workspace_path=tmp_path / "work")
                if result.requires_review:
                    checkpoint = dagent.RunCheckpoint.model_validate_json(result.checkpoint.model_dump_json())
                    result, _ = await resume_invoke(runner, result.review.approve(), checkpoint, host)
                assert result.status == "completed"
                assert host.state == result.conversation
                if scenario == "overflow":
                    assert host.resources
                if scenario == "compaction":
                    assert any(batch.context and batch.context.summary_change for batch in host.batches)
                if scenario == "nested":
                    assert any(record.origin.scope != "conversation" for batch in host.batches for record in batch.messages)
                streams.append([batch.model_dump(mode="json") for batch in host.batches])
    assert streams[0] == streams[1]
