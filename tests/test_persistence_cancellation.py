"""ACK and cancellation ordering through the public persistent stream API."""

import asyncio
from contextlib import aclosing, closing

import pytest

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall
from tests.test_conversation_changes import Host


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_first", [False, True])
@pytest.mark.parametrize("cancel_at", ["input", "tool_result"])
async def test_cancel_pending_ack_keeps_confirmed_revision(tmp_path, ack_first, cancel_at):
    effects = []

    @dagent.tool
    def record() -> str:
        effects.append("once")
        return "recorded"

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="record", name="tool_record")]),
                             ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        requested = False
        terminals = []
        async with aclosing(runner.stream(dagent.ToolAgent(profile="conversation", capabilities=[record]),
                input="record", run_id="cancel", persistence=dagent.StreamPersistenceOptions())) as stream:
            async for event in stream:
                assert event.type != "run.failed"
                if event.type != "conversation.changed":
                    continue
                batch = event.data
                await host.save(batch)
                delivery = runner._persistent_deliveries[batch.run_id]
                target = batch.sequence == 1 if cancel_at == "input" else any(
                    isinstance(record.item, dagent.ToolResultMessage) for record in batch.messages)
                if target and not requested:
                    if ack_first:
                        assert await runner.acknowledge_changes(batch.run_id, batch.batch_id)
                    assert await runner.cancel(batch.run_id)
                    assert await runner.cancel(batch.run_id)
                    requested = True
                if batch.status is not None:
                    terminals.append(batch)
                    assert not await runner.cancel(batch.run_id)
                assert await runner.acknowledge_changes(batch.run_id, batch.batch_id)

        assert requested and [batch.status for batch in terminals] == ["cancelled"]
        assert terminals[0].revision == host.state.revision
        assert delivery.acknowledged_revision == host.state.revision
        assert delivery.failed_batch_id is None
        assert effects == ([] if cancel_at == "input" else ["once"])
        assert len(provider.requests) == (0 if cancel_at == "input" else 1)
        assert not runner._persistent_deliveries and not runner._active_run_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "awaiting_review"])
@pytest.mark.parametrize("action", ["ack", "reject", "timeout", "close", "runner_close"])
async def test_terminal_delivery_cannot_be_cancelled_and_releases_review(tmp_path, status, action):
    effects = []

    @dagent.tool(risk="medium")
    def record() -> str:
        effects.append("unexpected")
        return "recorded"

    response = {"completed": ChatResponse(content="done"), "failed": ChatResponse(),
                "awaiting_review": ChatResponse(tool_calls=[ToolCall(id="record", name="tool_record")])}[status]
    provider = MockProvider([response])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        observed = []
        terminals = []
        async with aclosing(runner.stream(dagent.ToolAgent(profile="conversation", capabilities=[record], review="careful"),
                input="record", run_id="terminal", persistence=dagent.StreamPersistenceOptions(
                    ack_timeout_seconds=.03 if action == "timeout" else None))) as stream:
            async for event in stream:
                observed.append(event)
                if event.type != "conversation.changed":
                    continue
                batch = event.data
                await host.save(batch)
                if batch.status is None:
                    assert await runner.acknowledge_changes(batch.run_id, batch.batch_id)
                    continue
                terminals.append(batch)
                assert batch.status == status
                assert not await runner.cancel(batch.run_id)
                assert not await runner.cancel(batch.run_id)
                if action == "ack":
                    assert await runner.acknowledge_changes(batch.run_id, batch.batch_id)
                    assert await runner.acknowledge_changes(batch.run_id, batch.batch_id)
                elif action == "reject":
                    assert await runner.reject_changes(batch.run_id, batch.batch_id, message="storage failed")
                elif action in {"close", "runner_close"}:
                    if action == "runner_close":
                        runner.close()
                    await asyncio.wait_for(stream.aclose(), 1)
                    break

        assert len(terminals) == 1 and effects == []
        if action in {"reject", "timeout"}:
            assert observed[-1].type == "run.failed"
            assert observed[-1].data.error_type == (
                "PersistenceRejected" if action == "reject" else "PersistenceTimeout")
            assert observed[-1].data.failed_batch_id == terminals[0].batch_id
            assert observed[-1].data.acknowledged_sequence == terminals[0].sequence - 1
        else:
            assert not any(event.type == "run.failed" for event in observed)
        if status == "awaiting_review":
            assert runner.run_state("terminal") is None
            assert runner.run_checkpoint("terminal") is None
            assert "terminal" not in runner.runtime.session._review_index.values()
        assert not runner._persistent_deliveries and not runner._active_run_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reject", "timeout", "close"])
async def test_cancel_does_not_confirm_failed_delivery(tmp_path, action):
    provider = MockProvider([ChatResponse(content="must not run")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        async with aclosing(runner.stream(dagent.ToolAgent(profile="conversation"), input="hi", run_id="unconfirmed",
                persistence=dagent.StreamPersistenceOptions(
                    ack_timeout_seconds=.03 if action == "timeout" else None))) as stream:
            async for event in stream:
                if event.type == "conversation.changed":
                    pending = event.data
                    break
            delivery = runner._persistent_deliveries[pending.run_id]
            assert await runner.cancel(pending.run_id)
            assert await runner.cancel(pending.run_id)
            if action == "close":
                await asyncio.wait_for(stream.aclose(), 1)
            else:
                if action == "reject":
                    assert await runner.reject_changes(pending.run_id, pending.batch_id, message="storage failed")
                events = await asyncio.wait_for(collect(stream), 1)
                assert events[-1].type == "run.failed"
                assert events[-1].data.error_type == (
                    "PersistenceRejected" if action == "reject" else "PersistenceTimeout")
                assert events[-1].data.failed_batch_id == pending.batch_id
                assert not any(event.type == "conversation.changed" for event in events)
        assert delivery.acknowledged_sequence == 0 and delivery.acknowledged_revision == 0
        assert provider.requests == []
        assert not runner._persistent_deliveries and not runner._active_run_tasks


async def collect(stream):
    return [event async for event in stream]
