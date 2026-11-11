"""Provider call ids identify a tool round, not an entire run's trace."""

import asyncio
from contextlib import closing

import pytest

import dagent
from dagent.capabilities.tools.registry import ToolOutput
from dagent.harness_runtime import result_storage
from dagent.harness_runtime.tool_agent import _find_capability_node, _restore_result_nodes
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import RunTrace, RunTraceNode
from tests.test_steering import GatedProvider
from tests.test_conversation_changes import Host, invoke


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("call_id", ["duplicate", "", " "])
async def test_custom_duplicate_ids_fail_without_dangling_calls(tmp_path, stream, call_id):
    executed = []

    @dagent.tool
    def echo() -> str:
        executed.append(True)
        return "effect"

    calls = [ToolCall(id=call_id, name="tool_echo", arguments={})] * 2
    bad = ChatResponse(content="partial", reasoning_content="thought", tool_calls=calls,
                       usage=dagent.ModelTokenUsage(input_tokens=3, output_tokens=2, total_tokens=5))
    provider = MockProvider([bad, ChatResponse(content="continued")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        agent = dagent.ToolAgent(profile="conversation", capabilities=[echo])
        result, events = await invoke(runner, agent, host, stream, input="echo")
        assert result.status == "failed" and result.error.code == "invalid_tool_call"
        assert executed == [] and host.state == result.conversation
        records = [r.item for b in host.batches for r in b.messages if r.partial]
        assert len(records) == 1
        assert [call.model_dump(include={"id", "name", "arguments"}) for call in records[0].tool_calls] == [
            {"id": call.id, "name": call.name, "arguments": call.arguments} for call in calls]
        assert records[0].content == "partial" and records[0].reasoning == "thought"
        assert records[0].usage.total_tokens == 5
        assert all(not isinstance(i, dagent.AssistantMessage) for i in result.conversation.items)
        assert any(o.error and o.error.code == "invalid_tool_call" for b in host.batches for o in b.observations if o.type == "response")
        if stream:
            assert events[-1].type == "conversation.changed" and events[-1].data.status == "failed"
        continued, _ = await invoke(runner, agent, host, stream, input="continue", conversation=result.conversation)
        assert continued.status == "completed" and host.state == continued.conversation
        assert all(not item.get("tool_calls") for item in provider.requests[-1]["messages"])
        assert bad.tool_calls == calls and bad.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("call_id", ["duplicate", "", " "])
async def test_custom_duplicate_ids_fail_in_ordinary_run_and_stream(tmp_path, stream, call_id):
    executed = []

    @dagent.tool
    def echo() -> str:
        executed.append(True)
        return "effect"

    calls = [ToolCall(id=call_id, name="tool_echo", arguments={})] * 2
    bad = ChatResponse(content="partial", reasoning_content="thought", tool_calls=calls,
                       usage=dagent.ModelTokenUsage(input_tokens=3, output_tokens=2, total_tokens=5))
    provider = MockProvider([bad, ChatResponse(content="continued")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        agent = dagent.ToolAgent(profile="conversation", capabilities=[echo])
        if stream:
            events = [event async for event in runner.stream(agent, input="echo")]
            assert events[-1].type == "run.finished"
            result = events[-1].data.result
        else:
            result = await runner.run(agent, input="echo")
        assert result.status == "failed" and result.error.code == "invalid_tool_call"
        assert executed == []
        last = result.conversation.items[-1]
        assert not last.tool_calls and last.content == "partial" and last.reasoning == "thought"
        node = result.trace.root.children[-1]
        assert node.output["usage"]["total_tokens"] == 5
        assert node.output["provider_details"]["tool_calls"] == [
            {"id": call.id, "name": call.name, "arguments": call.arguments} for call in calls]
        continued = await runner.run(agent, input="continue", conversation=result.conversation)
        assert continued.status == "completed"
        assert all(not item.get("tool_calls") for item in provider.requests[-1]["messages"])
        assert bad.tool_calls == calls and bad.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("call_id,name", [("", "tool_echo"), (" ", "tool_echo"), ("call", ""), ("call", "unknown")])
async def test_custom_single_call_identity_contract(tmp_path, call_id, name):
    @dagent.tool
    def echo() -> str:
        return "effect"

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id=call_id, name=name)]), ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[echo]), input="echo")
    assert result.status == "completed"
    message = provider.requests[-1]["messages"][-1]
    assert message["tool_call_id"] == call_id
    assert ("[TOOL_ERROR]" in message["content"]) == (name != "tool_echo")


def _calls(result):
    return [node for node in result.trace.root.children if node.kind == "capability_call"]


@pytest.mark.asyncio
@pytest.mark.parametrize("max_inline_bytes", [1024, 256 * 1024])
async def test_repeated_call_ids_keep_each_execution_and_overflow(tmp_path, max_inline_bytes):
    @dagent.tool
    def echo(label: str) -> str:
        return label + " evidence" * 200

    provider = MockProvider([
        *(ChatResponse(tool_calls=[ToolCall(id="call_0", name="tool_echo", arguments={"label": label})])
          for label in ("first", "second")),
        ChatResponse(content="done"),
    ])
    with closing(dagent.Runner(
        workspace=tmp_path, provider=provider, skill_roots=[],
        result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=max_inline_bytes),
    )) as runner:
        result = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[echo, "tool.read_file"], context=dagent.ContextPolicy(max_tool_result_tokens=64)), input="echo twice")
        nodes = _calls(result)
        items = [item for item in result.conversation.items if isinstance(item, dagent.ToolResultMessage)]
        assert len(nodes) == len(items) == 2
        assert nodes[0].id != nodes[1].id
        assert items[0].content.path != items[1].content.path
        for node, item, label in zip(nodes, items, ("first", "second"), strict=True):
            execution = node.capability_execution
            assert node.status == "completed" and node.ended_at is not None
            assert execution.invocation.arguments == {"label": label}
            assert execution.invocation.invocation_id == item.call_id == "call_0"
            assert execution.result.content_reference == item.content
            assert execution.result.retention == item.retention
            path = runner.resolve_runtime_path(item.content.path, workspace_path=result.state.workspace_path)
            assert path.read_text() == label + " evidence" * 200


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_repeated_call_id_review_resumes_the_pending_node(tmp_path, approved):
    executed = []

    @dagent.tool
    def inspect() -> str:
        return "inspected"

    @dagent.tool(risk="medium")
    def record(label: str) -> str:
        executed.append(label)
        return label

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_0", name="tool_inspect", arguments={})]),
        ChatResponse(tool_calls=[ToolCall(id="call_0", name="tool_record", arguments={"label": "second"})]),
        ChatResponse(content="done"),
    ])
    with closing(dagent.Runner(workspace=tmp_path, provider=provider, skill_roots=[])) as runner:
        first = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[inspect, record], review="careful"), input="record")
    assert first.requires_review
    old, pending = _calls(first)
    assert old.status == "completed" and pending.status == "awaiting_review"
    checkpoint = dagent.RunCheckpoint.model_validate_json(first.checkpoint.model_dump_json())
    tampered = checkpoint.model_dump(mode="json")
    tampered["state"]["model_thread"]["items"].append(dagent.ToolResultMessage(
        call_id="call_0", name="tool_record", status="completed",
    ).model_dump(mode="json"))
    with pytest.raises(ValueError, match="tool results do not match its batch cursor"):
        dagent.RunCheckpoint.model_validate(tampered)
    with closing(dagent.Runner(workspace=tmp_path, provider=provider, capabilities=[inspect, record], skill_roots=[])) as runner:
        result = await runner.resume(first.review.approve() if approved else first.review.reject(), checkpoint=checkpoint)
    previous, resumed = _calls(result)
    assert previous == old
    assert resumed.id == pending.id
    assert resumed.capability_execution.invocation.arguments == {"label": "second"}
    assert resumed.status == ("completed" if approved else "failed")
    assert executed == (["second"] if approved else [])


@pytest.mark.asyncio
async def test_steer_skipping_reused_call_id_preserves_completed_node(tmp_path):
    @dagent.tool
    def echo() -> str:
        return "first result"

    provider = GatedProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_0", name="tool_echo", arguments={})]),
        ChatResponse(tool_calls=[ToolCall(id="call_0", name="tool_echo", arguments={})]),
        ChatResponse(content="redirected"),
    ], blocked={1})
    with closing(dagent.Runner(workspace=tmp_path, provider=provider, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(dagent.ToolAgent(profile="conversation", capabilities=[echo]), input="echo", run_id="steered"))
        await asyncio.wait_for(provider.started[1].wait(), 5)
        await runner.steer("steered", "stop calling echo")
        provider.release[1].set()
        result = await asyncio.wait_for(task, 5)
    assert [node.status for node in _calls(result)] == ["completed"]
    assert _calls(result)[0].capability_execution.result.content == "first result"
    assert [item.status for item in result.conversation.items
            if isinstance(item, dagent.ToolResultMessage)] == ["completed", "skipped"]


@pytest.mark.asyncio
async def test_storage_failure_with_reused_call_id_preserves_first_result(tmp_path, monkeypatch):
    @dagent.tool
    def produce(binary: bool):
        return ToolOutput("second", value=b"binary") if binary else "first"

    provider = MockProvider([
        *(ChatResponse(tool_calls=[ToolCall(id="call_0", name="tool_produce", arguments={"binary": binary})])
          for binary in (False, True)),
    ])

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(result_storage, "_write_reference", fail)
    with closing(dagent.Runner(workspace=tmp_path, provider=provider, skill_roots=[])) as runner:
        with pytest.raises(dagent.RunExecutionError) as raised:
            await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[produce]), input="produce")
    first, failed = _calls(raised.value.result)
    assert first.status == "completed"
    assert first.capability_execution.result.content == "first"
    assert first.error is None
    assert failed.status == "failed" and failed.error.code == "ResultStorageError"
    assert failed.capability_execution.invocation.arguments == {"binary": True}


def _node(call_id):
    invocation = dagent.CapabilityInvocation(invocation_id=call_id, capability_id="tool.echo", kind="tool")
    return RunTraceNode.capability_call(parent_id="root", invocation=invocation)


def test_node_lookup_uses_latest_unfinished_call_in_current_loop_only():
    old, nested, first, latest = (_node("call_0") for _ in range(4))
    old.ended_at = old.started_at
    root = RunTraceNode.run(run_id="run")
    root.children = [old, first, latest, RunTraceNode(kind="agent_loop", children=[nested])]
    assert _find_capability_node(root, "call_0") is latest
    latest.ended_at = latest.started_at
    assert _find_capability_node(root, "call_0") is first
    first.ended_at = first.started_at
    assert _find_capability_node(root, "call_0") is None


def test_checkpoint_result_associations_require_unique_ids_on_both_sides():
    unique, duplicate, pending, nested = (_node(call_id) for call_id in ("unique", "duplicate", "duplicate", "unique"))
    trace = RunTrace(run_id="run", root=RunTraceNode.run(run_id="run"))
    trace.root.children = [unique, duplicate, pending, RunTraceNode(kind="agent_loop", children=[nested])]
    items = tuple(dagent.ToolResultMessage(id=label, run_id=run_id, call_id=call_id, name="tool_echo", status="completed")
                  for label, run_id, call_id in (("one", "run", "unique"), ("two", "run", "duplicate"),
                                                ("other_run", "old", "unique")))
    conversation = dagent.ConversationState(items=items)
    assert _restore_result_nodes(trace, conversation) == {"one": unique}
    ambiguous = conversation.model_copy(update={
        "items": (*items, items[0].model_copy(update={"id": "another"})),
    })
    assert _restore_result_nodes(trace, ambiguous) == {}
