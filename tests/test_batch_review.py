"""Whole model rounds retain order and individual review decisions across resume."""

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


def run(awaitable):
    return asyncio.run(awaitable)


def _results(result):
    return [item for item in result.state.model_thread.items if isinstance(item, dagent.ToolResultMessage)]


@pytest.mark.parametrize("approvals", [(True, True, True), (False, False, False), (True, False, True)])
def test_batch_review_is_atomic_before_execution_and_resumes_in_order(tmp_path, approvals):
    executed = []

    @dagent.tool(risk="medium")
    def record(label: str) -> str:
        executed.append(label)
        return label

    @dagent.tool
    def inspect() -> str:
        executed.append("inspect")
        return "ready"

    provider = MockProvider([
        ChatResponse(tool_calls=[
            ToolCall(id="inspect", name="tool_inspect", arguments={}),
            *(ToolCall(id=f"call_{i}", name="tool_record", arguments={"label": str(i)}) for i in range(3)),
        ]),
        ChatResponse(content="finished"),
    ])
    runner = dagent.Runner(workspace=tmp_path, provider=provider, skill_roots=[])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[inspect, record], review="careful")
    first = run(runner.run(agent, input="inspect and record"))
    assert executed == []
    assert len(provider.requests) == 1
    assert first.usage.capability_calls == 0
    assert first.review.capability_call is None
    assert len(first.review.capability_calls) == 3
    assert first.pending_review.queued_call_count == 1
    assert _results(first) == []
    checkpoint = dagent.RunCheckpoint.model_validate_json(first.checkpoint.model_dump_json())
    decision = first.review.decide([
        dagent.CapabilityReviewDecision(invocation_id=f"call_{i}", approved=approved)
        for i, approved in reversed(list(enumerate(approvals)))
    ], feedback="Use these choices.")
    second = dagent.Runner(workspace=tmp_path, provider=provider, capabilities=[inspect, record], skill_roots=[])
    try:
        result = run(second.resume(decision, checkpoint=checkpoint))
        assert result.status == "completed"
        assert executed == ["inspect", *(str(i) for i, approved in enumerate(approvals) if approved)]
        items = _results(result)
        assert [item.call_id for item in items] == ["inspect", "call_0", "call_1", "call_2"]
        assert [item.status for item in items] == ["completed", *("completed" if value else "denied" for value in approvals)]
        assert all("Use these choices." in item.content.text for item in items[1:])
        assert result.usage.capability_calls == 1 + sum(approvals)
        assert result.usage.model_turns == len(provider.requests) == 2
        assert len([node for node in result.trace.root.children if node.capability_execution]) == 4
        with pytest.raises(ValueError, match="already been consumed"):
            run(second.resume(decision, checkpoint=checkpoint))
    finally:
        runner.close()
        second.close()


def test_incomplete_decision_does_not_consume_review_or_run_tools(tmp_path):
    executed = []

    @dagent.tool(risk="high")
    def publish(label: str) -> str:
        executed.append(label)
        return label

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id=str(i), name="tool_publish", arguments={"label": str(i)}) for i in range(2)]),
        ChatResponse(content="done"),
    ])
    runner = dagent.Runner(workspace=tmp_path, provider=provider, skill_roots=[])
    try:
        first = run(runner.run(dagent.ToolAgent(profile="conversation", capabilities=[publish], review="careful"), input="publish"))
        for ids in [("0",), ("0", "unknown")]:
            decision = dagent.ReviewDecision(review_id=first.review.review_id, capability_decisions=tuple(
                dagent.CapabilityReviewDecision(invocation_id=id, approved=True) for id in ids
            ))
            with pytest.raises(ValueError, match="exactly the pending"):
                run(runner.resume(decision, checkpoint=first.checkpoint))
            assert executed == []
            assert len(provider.requests) == 1
        result = run(runner.resume(first.review.approve(), checkpoint=first.checkpoint))
        assert result.status == "completed"
        assert executed == ["0", "1"]
    finally:
        runner.close()


def test_decision_contract_rejects_ambiguous_duplicate_and_non_boolean_inputs():
    item = dagent.CapabilityReviewDecision(invocation_id="x", approved=True)
    with pytest.raises(ValueError, match="not both"):
        dagent.ReviewDecision(review_id="review", approved=True, capability_decisions=(item,))
    with pytest.raises(ValueError, match="unique"):
        dagent.ReviewDecision(review_id="review", capability_decisions=(item, item))
    with pytest.raises(ValidationError):
        dagent.CapabilityReviewDecision(invocation_id="x", approved="false")


def test_ordinary_failure_and_hard_denial_do_not_stop_approved_siblings(tmp_path):
    executed = []

    @dagent.tool(risk="medium")
    def publish(label: str) -> str:
        executed.append(label)
        if label == "bad":
            raise ValueError("ordinary failure")
        return label

    provider = MockProvider([
        ChatResponse(tool_calls=[
            ToolCall(id="blocked", name="tool_shell", arguments={"command": "rm -rf /"}),
            ToolCall(id="bad", name="tool_publish", arguments={"label": "bad"}),
            ToolCall(id="good", name="tool_publish", arguments={"label": "good"}),
        ]),
        ChatResponse(content="done"),
    ])
    runner = dagent.Runner(workspace=tmp_path, provider=provider, skill_roots=[])
    try:
        first = run(runner.run(dagent.ToolAgent(profile="conversation", capabilities=[publish, "tool.shell"], review="careful"), input="run"))
        assert [item.invocation_id for item in first.review.capability_calls] == ["bad", "good"]
        assert executed == []
        async def resume():
            return [event async for event in runner.resume_stream(first.review.approve(), checkpoint=first.checkpoint)]
        events = run(resume())
        result = events[-1].data.result
        assert executed == ["bad", "good"]
        assert result.usage.capability_calls == 2
        assert [item.status for item in _results(result)] == ["failed", "failed", "completed"]
        assert [(event.type, event.data.invocation_id) for event in events if event.type in {"capability.call.failed", "capability.call.completed"}] == [
            ("capability.call.failed", "blocked"), ("capability.call.failed", "bad"), ("capability.call.completed", "good"),
        ]
    finally:
        runner.close()


def test_changed_boundary_rechecks_saved_queue_without_replaying_completed_calls(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    safe = workspace / "safe.txt"
    safe.write_text("safe")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    alias = workspace / "alias.txt"
    alias.symlink_to(safe)
    executed = []

    @dagent.tool
    def redirect() -> str:
        executed.append("redirect")
        alias.unlink()
        alias.symlink_to(outside)
        return "redirected"

    @dagent.tool(risk="medium")
    def finish() -> str:
        executed.append("finish")
        return "finished"

    provider = MockProvider([
        ChatResponse(tool_calls=[
            ToolCall(id="redirect", name="tool_redirect", arguments={}),
            ToolCall(id="read", name="tool_read_file", arguments={"path": "alias.txt"}),
            ToolCall(id="finish", name="tool_finish", arguments={}),
        ]),
        ChatResponse(content="done"),
    ])
    runner = dagent.Runner(workspace=workspace, provider=provider, skill_roots=[])
    try:
        first = run(runner.run(dagent.ToolAgent(profile="conversation", capabilities=[redirect, finish, "tool.read_file"], review="careful"), input="run", workspace_path=workspace))
        assert executed == []
        second = run(runner.resume(first.review.approve(), checkpoint=first.checkpoint))
        assert second.status == "awaiting_review"
        assert executed == ["redirect"]
        assert second.review.capability_call["invocation_id"] == "read"
        assert second.pending_review.payload["reason"] == "boundary_violation"
        assert len(provider.requests) == 1
        checkpoint = dagent.RunCheckpoint.model_validate_json(second.checkpoint.model_dump_json())
        third = run(runner.resume(second.review.approve(), checkpoint=checkpoint))
        assert third.status == "completed"
        assert executed == ["redirect", "finish"]
        assert [item.call_id for item in _results(third)] == ["redirect", "read", "finish"]
        assert _results(third)[1].content.text == "outside"
        assert third.usage.capability_calls == 3
    finally:
        runner.close()


def test_static_agent_batch_restores_child_and_runs_downstream_once(tmp_path):
    executed = []

    @dagent.tool(risk="medium")
    def record(label: str) -> str:
        executed.append(label)
        return label

    @dagent.tool
    def downstream() -> str:
        executed.append("downstream")
        return "done"

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id=str(i), name="tool_record", arguments={"label": str(i)}) for i in range(3)]),
        ChatResponse(content="done"),
    ])
    agent = dagent.ToolAgent(name="helper", profile="conversation", capabilities=[record])
    dag = dagent.Dag("batch")
    dag.add_node(dagent.Node("assistant", target=agent, inputs={"prompt": "record"}))
    dag.add_node(dagent.Node("after", target=downstream))
    dag.add_edge("assistant", "after")
    runner = dagent.Runner(workspace=tmp_path, provider=provider, skill_roots=[])
    try:
        first = run(runner.run(dag, review="careful"))
        assert executed == []
        assert first.state.pending_tool_batch is None
        assert first.state.static_agent_continuation.agent_state.pending_tool_batch is not None
        checkpoint = dagent.RunCheckpoint.model_validate_json(first.checkpoint.model_dump_json())
        result = run(runner.resume(first.review.decide([
            dagent.CapabilityReviewDecision(invocation_id=str(i), approved=i != 1) for i in range(3)
        ]), checkpoint=checkpoint))
        assert executed == ["0", "2", "downstream"]
        assert result.node_output("after") == "done"
        assert result.status == "completed"
    finally:
        runner.close()
