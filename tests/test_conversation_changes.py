"""A host reducer independent of the SDK's state-diff implementation."""

from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import json

import pytest

import dagent
from dagent.providers import ChatResponse, ChatStreamEvent, MockProvider, ToolCall
from dagent.schemas.conversation_changes import ConversationChangeBatch
from tests.planner_helpers import final_answer_response


class Host:
    def __init__(self, initial=None, runner=None):
        self.state = initial
        self.runner = runner
        self.records = {} if initial is None else {(initial.id, item.id): item for item in initial.items}
        self.batches = []
        self.resources = {}
        self.sequences = {}

    async def save(self, batch):
        batch = ConversationChangeBatch.model_validate_json(batch.model_dump_json())
        assert batch.sequence == self.sequences.get(batch.delivery_id, 0) + 1
        self.sequences[batch.delivery_id] = batch.sequence
        if batch.conversation_id is not None:
            if self.state is None:
                self.state = dagent.ConversationState(id=batch.conversation_id)
            assert self.state.id == batch.conversation_id
            assert self.state.revision == batch.base_revision
        for record in batch.messages:
            self.records[(record.origin.id, record.item.id)] = record.item
        for resource in batch.resources:
            path = self.runner.resolve_runtime_path(
                resource.reference.path, workspace_path=resource.workspace_path,
                runtime_directory=resource.runtime_directory,
            )
            body = path.read_bytes()
            assert len(body) == resource.reference.byte_length
            # Only compaction archives carry a digest; tool output files do not.
            if resource.reference.sha256 is not None:
                assert hashlib.sha256(body).hexdigest() == resource.reference.sha256
            self.resources[(resource.workspace_path, resource.reference.path)] = body
        if batch.context is not None:
            change = batch.context
            remove = set(change.deactivated_item_ids)
            summary = self.state.summary
            if change.summary_change is not None:
                remove.update(change.summary_change.replaced_item_ids)
                summary = change.summary_change.value
            assert remove <= {item.id for item in self.state.items}
            items = [item for item in self.state.items if item.id not in remove]
            updates = set(change.updated_item_ids)
            items = [self.records[(self.state.id, item.id)] if item.id in updates else item for item in items]
            for placement in change.added:
                items.insert(placement.index, self.records[(self.state.id, placement.item_id)])
            compaction = self.state.compaction_state
            if change.compaction_state_change is not None:
                compaction = change.compaction_state_change.value
            self.state = dagent.ConversationState(
                id=batch.conversation_id, revision=batch.revision, items=tuple(items),
                summary=summary, compaction_state=compaction,
            )
        self.batches.append(batch)


class PersistedTurn:
    """Host projection assembled exclusively from public stream records."""

    def __init__(self, host, terminal, batches):
        self.conversation = host.state
        self.run_id = terminal.run_id
        self.status = terminal.status
        self.usage = terminal.usage
        self.context_usage = terminal.context_usage
        self.error = terminal.error
        self.checkpoint = terminal.checkpoint
        self.review = None if self.checkpoint is None else self.checkpoint.review
        self.requires_review = self.status == "awaiting_review"
        self.plan = None if self.checkpoint is None else self.checkpoint.plan
        self.trace = None
        self.new_items = tuple({(r.origin.id, r.item.id): r.item for b in batches for r in b.messages}.values())
        self.output_text = terminal.output_text or ""
        if terminal.output_item_id is not None:
            item = host.records[(terminal.conversation_id, terminal.output_item_id)]
            self.output_text = item.content.strip() or item.refusal.strip()


async def consume(runner, events, host):
    observed = []
    batches = []
    terminal = None
    async for event in events:
        observed.append(event)
        if event.type == "conversation.changed":
            batch = event.data
            batches.append(batch)
            try:
                await host.save(batch)
            except Exception as exc:
                assert await runner.reject_changes(batch.run_id, batch.batch_id, message=str(exc))
            else:
                assert await runner.acknowledge_changes(batch.run_id, batch.batch_id)
            if batch.status is not None:
                terminal = batch
    if terminal is None:
        return None, observed
    return PersistedTurn(host, terminal, batches), observed


async def invoke(runner, agent, host, stream, **kwargs):
    result, events = await consume(runner, runner.stream(agent, persistence=dagent.StreamPersistenceOptions(), **kwargs), host)
    assert result is not None, events[-1] if events else "empty stream"
    return result, events if stream else []


async def resume_invoke(runner, decision, checkpoint, host):
    return await consume(runner, runner.resume_stream(decision, checkpoint=checkpoint,
                         persistence=dagent.StreamPersistenceOptions()), host)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_replay_tool_records_and_continue_without_saving_result(tmp_path, stream):
    executed = []

    @dagent.tool
    def echo(text: str) -> dict:
        executed.append(text)
        return {"text": text}

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call", name="tool_echo", arguments={"text": "value"})]),
        ChatResponse(content="done", reasoning_content="checked"), ChatResponse(content="next"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        agent = dagent.ToolAgent(profile="conversation", capabilities=[echo])
        result, events = await invoke(runner, agent, host, stream, input="first", run_id="first")
        assert host.state == result.conversation
        assert host.state.model_dump_json() == result.conversation.model_dump_json()
        assert executed == ["value"]
        if events:
            started = [event.data for event in events if event.type == "response.started"]
            assistants = [item for item in host.state.items if isinstance(item, dagent.AssistantMessage)]
            assert [item.message_id for item in started] == [item.id for item in assistants]
            links = {obs.response_id: obs.message_id for batch in host.batches for obs in batch.observations if obs.type == "response"}
            assert all(links[event.data.response_id] == event.data.message_id for event in events
                       if event.type in {"response.content.delta", "response.reasoning.delta", "response.finished"})
        restored = dagent.ConversationState.model_validate_json(host.state.model_dump_json())
        second, _ = await invoke(runner, agent, host, stream, input="next", conversation=restored, run_id="second")
        assert host.state == second.conversation
        assert len(host.sequences) == 2


@pytest.mark.asyncio
async def test_subagent_audit_does_not_replace_parent_context(tmp_path):
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="delegate", name="agent_helper", arguments={"prompt": "help"})]),
        ChatResponse(content="child answer"), ChatResponse(content="parent answer"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        runner.add_agent(dagent.ToolAgent(name="helper", profile="conversation", capabilities=[], skills=[]))
        host = Host(runner=runner)
        result, _ = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[], agents=["agent.helper"]),
                                 host, True, input="delegate")
        assert host.state == result.conversation
        child = [record for batch in host.batches for record in batch.messages if record.origin.scope == "subagent"]
        assert any(record.item.content == "child answer" for record in child)
        assert not {record.item.id for record in child} & {item.id for item in host.state.items}


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
async def test_ready_files_are_notified_but_not_restored_into_another_workspace(tmp_path, external):
    body = "evidence\n" * 3000

    @dagent.tool
    def report() -> str:
        return body

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="report", name="tool_report", arguments={})]),
        ChatResponse(content="saved"), ChatResponse(content="continued"),
    ])
    directory = tmp_path / "private" if external else ".runtime"
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, runtime_directory=directory, skill_roots=[],
                              result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024))) as runner:
        host = Host(runner=runner)
        agent = dagent.ToolAgent(profile="conversation", capabilities=[report, "tool.read_file"])
        first, _ = await invoke(runner, agent, host, False, input="report", workspace_path=tmp_path / "first")
        assert host.state == first.conversation
        assert body.encode() in host.resources.values()
        saved = next(item for item in first.conversation.items if isinstance(item, dagent.ToolResultMessage))
        assert saved.content.sha256 is None
        second, _ = await invoke(runner, agent, host, False, input="continue", conversation=host.state,
                                 workspace_path=tmp_path / "second")
        assert host.state == second.conversation
        # Tool output keeps its original path and is not copied to the new workspace.
        assert not any(workspace == str(tmp_path / "second") for workspace, _ in host.resources)
        assert saved in second.conversation.items
        path = runner.resolve_runtime_path(saved.content.path, workspace_path=tmp_path / "second",
                                           runtime_directory=directory)
        assert not path.exists()


@pytest.mark.asyncio
async def test_long_initial_history_is_not_republished(tmp_path):
    initial = dagent.ConversationState(revision=10, items=tuple(
        item for index in range(5) for item in (
            dagent.UserMessage(content=f"old user {index}"), dagent.AssistantMessage(content=f"old answer {index}"),
        )
    ))
    with closing(dagent.Runner(provider=MockProvider([ChatResponse(content="new answer")]), workspace=tmp_path,
                              skill_roots=[])) as runner:
        host = Host(initial, runner)
        result, _ = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[]), host, False,
                                 input="new user", conversation=initial)
        assert host.state == result.conversation
        assert sum(len(batch.messages) for batch in host.batches) == 2
        encoded = "".join(batch.model_dump_json() for batch in host.batches)
        assert "old answer" not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_opt_in_does_not_change_provider_requests(tmp_path, stream):
    requests = []
    for enabled in [False, True]:
        provider = MockProvider([ChatResponse(content="answer", reasoning_content="reason")])
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            host = Host(runner=runner)
            agent = dagent.ToolAgent(profile="conversation", capabilities=[])
            arguments = dict(input="question", workspace_path=tmp_path / "session", run_id="wire")
            if enabled:
                await invoke(runner, agent, host, stream, **arguments)
            else:
                events = [event async for event in runner.stream(agent, **arguments)]
                assert all("message_id" not in event.model_dump(mode="json")["data"] for event in events)
            requests.append(provider.requests)
    assert requests[0] == requests[1]


@pytest.mark.asyncio
async def test_legacy_compaction_batches_exact_removals(tmp_path):
    initial = dagent.ConversationState(revision=8, items=tuple(
        item for index in range(4) for item in (
            dagent.UserMessage(run_id=f"old_{index}", content="question " * 200),
            dagent.AssistantMessage(run_id=f"old_{index}", content="evidence " * 300),
        )
    ))
    provider = MockProvider([ChatResponse(content="bounded summary"), ChatResponse(content="answer")])
    provider.configured_context_window_tokens = 4096
    provider.max_output_tokens = 512
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(initial, runner)
        agent = dagent.ToolAgent(profile="conversation", capabilities=[], context=dagent.ContextPolicy(
            compaction_trigger_ratio=.4, summary_max_tokens=128,
        ))
        result, _ = await invoke(runner, agent, host, False, input="next", conversation=initial)
        assert host.state == result.conversation
        replacements = [batch.context.summary_change for batch in host.batches
                        if batch.context is not None and batch.context.summary_change is not None]
        assert replacements
        assert set(replacements[0].replaced_item_ids) <= {item.id for item in initial.items}
        assert not any("old_" in record.item.run_id for batch in host.batches for record in batch.messages)


def test_optional_response_identity_preserves_positional_event_constructors():
    from dagent.result import ResponseStartedData, ResponseFinishedData, TextDeltaData
    from dagent.harness_runtime.runtime_events import ResponseStreamContext

    assert ResponseStartedData("response", 3).model_step == 3
    assert ResponseFinishedData("response", 3).model_step == 3
    assert TextDeltaData("body", "response", 3).model_step == 3
    assert ResponseStreamContext("run", "response", 3).model_step == 3


def call_item_updated(batch, state):
    calls = {item.id for item in state.items if isinstance(item, dagent.AssistantMessage) and item.tool_calls}
    return bool(calls & set(batch.context.updated_item_ids))


@pytest.mark.asyncio
async def test_nested_argument_mutation_stays_in_the_tool_and_continues(tmp_path):
    seen = []

    @dagent.tool
    def append(values: list) -> str:
        values.append("added")
        seen.append(list(values))
        return "done"

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="append", name="tool_append", arguments={"values": ["original"]})]),
        ChatResponse(content="done"), ChatResponse(content="continued"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        agent = dagent.ToolAgent(profile="conversation", capabilities=[append])
        result, _ = await invoke(runner, agent, host, False, input="append")
        assert host.state == result.conversation
        call = next(item for item in host.state.items if isinstance(item, dagent.AssistantMessage)).tool_calls[0]
        # The tool changed its own copy; the call stays as the model made it.
        assert seen == [["original", "added"]]
        assert call.arguments == {"values": ["original"]}
        assert not any(batch.context and call_item_updated(batch, host.state) for batch in host.batches)
        restored = dagent.ConversationState.model_validate_json(host.state.model_dump_json())
        await invoke(runner, agent, host, False, input="continue", conversation=restored)
        first_call = next(message for message in provider.requests[1]["messages"] if message.get("tool_calls"))
        restored_call = next(message for message in provider.requests[2]["messages"] if message.get("tool_calls"))
        assert first_call == restored_call



@pytest.mark.asyncio
async def test_review_checkpoint_is_public_trace_free_and_released(tmp_path, monkeypatch):
    effects = []

    @dagent.tool(risk="medium")
    def record() -> str:
        effects.append("once")
        return "saved"

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="record", name="tool_record")]),
                             ChatResponse(content="finished")])
    from dagent.schemas.run_trace import RunTraceNode
    monkeypatch.setattr(RunTraceNode, "run", lambda **kwargs: pytest.fail("trace constructed"))
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        first, _ = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[record], review="careful"),
                                host, True, input="record")
        assert first.requires_review and effects == []
        saved = dagent.RunCheckpoint.model_validate_json(first.checkpoint.model_dump_json())
        assert saved.state.trace is None
        assert saved.pending_tool_invocations[0].capability_id == "tool.record"
        runner.validate_checkpoint(saved)
        assert runner.run_checkpoint(first.run_id) is None
        assert runner.run_state(first.run_id) is None
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[], capabilities=[record])) as runner:
        host.runner = runner
        result, _ = await resume_invoke(runner, saved.review.approve(), saved, host)
        assert result.status == "completed" and effects == ["once"]
        assert len(host.sequences) == 2
        assert {batch.run_id for batch in host.batches} == {first.run_id}
        assert runner.run_checkpoint(first.run_id) is None
        with pytest.raises(ValueError, match="consumed"):
            await runner.resume(saved.review.approve(), checkpoint=saved)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", ["response_start", "result", "validation_start", "validation_result"])
@pytest.mark.parametrize("attempts", [1, 2])
@pytest.mark.parametrize("resume", [False, True])
async def test_retry_delivery_failure_preserves_execution_facts(tmp_path, fail_at, attempts, resume):
    executed = []

    @dagent.tool(risk="medium" if resume else "low")
    def prepare() -> str:
        executed.append("prepare")
        return "prepared"

    @dagent.tool
    def retry(value: str) -> str:
        executed.append(value)
        return value

    responses = [ChatResponse(tool_calls=[ToolCall(id="prepare", name="tool_prepare")])]
    for attempt in range(attempts):
        responses.extend([ChatResponse(content=f"answer {attempt}"),
                          ChatResponse(content=json.dumps({"passed": False, "summary": "Try again", "issues": []})),
                          ChatResponse(tool_calls=[ToolCall(id=f"retry_{attempt}", name="tool_retry",
                                                         arguments={"value": f"retry_{attempt}"})])])
    if fail_at.startswith("validation_"):
        responses.extend([ChatResponse(content="final"), ChatResponse(content='{"passed":true,"issues":[]}')])
    provider = MockProvider(responses)
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[], validator="validator_agent")) as runner:
        runner.runtime.max_validation_retries = attempts
        host = Host(runner=runner)
        save = host.save
        feedback = set()

        async def rejecting(batch):
            await save(batch)
            feedback.update(r.item.id for r in batch.messages if isinstance(r.item, dagent.UserMessage)
                            and r.item.scope == "validator" and r.origin.scope == "conversation")
            if len(feedback) != attempts:
                return
            reject = (any(o.type == "response" and o.status == "started" for o in batch.observations)
                      if fail_at == "response_start" else
                      any(isinstance(r.item, dagent.ToolResultMessage) for r in batch.messages)
                      if fail_at == "result" else
                      any(o.type == "response" and o.status == "started" and o.origin.scope == "validator" for o in batch.observations)
                      if fail_at == "validation_start" else
                      any(isinstance(r.item, dagent.AssistantMessage) and r.item.scope == "validator" for r in batch.messages))
            if reject:
                raise OSError("commit failed")
        host.save = rejecting
        agent = dagent.ToolAgent(profile="conversation", capabilities=[prepare, retry], review="careful" if resume else "fast")
        if resume:
            pending, _ = await invoke(runner, agent, host, True, input="prepare")
            _, events = await resume_invoke(runner, pending.review.approve(), pending.checkpoint, host)
        else:
            _, events = await consume(runner, runner.stream(agent, input="prepare", persistence=dagent.StreamPersistenceOptions()), host)
        assert events[-1].type == "run.failed"
        assert events[-1].data.error_type == "PersistenceRejected"
        assert events[-1].data.result is None
        assert events[-1].data.failed_batch_id == host.batches[-1].batch_id
        assert executed == ["prepare", *(f"retry_{i}" for i in range(attempts - (fail_at == "response_start")))]
        assert runner.run_checkpoint(host.batches[-1].run_id) is None
        assert len(provider.requests) == len(responses) - (fail_at in {"response_start", "validation_start"})


@pytest.mark.asyncio
async def test_ack_precedes_model_timeout_and_close_do_not_hang(tmp_path):
    provider = MockProvider([ChatResponse(content="must not generate")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        agent = dagent.ToolAgent(profile="conversation")
        events = [event async for event in runner.stream(agent, input="hi", run_id="timeout",
                  persistence=dagent.StreamPersistenceOptions(ack_timeout_seconds=.01))]
        assert provider.requests == []
        assert events[-1].type == "run.failed"
        assert events[-1].data.error_type == "PersistenceTimeout"
        assert events[-1].data.acknowledged_sequence == 0
        stream = runner.stream(agent, input="hi", run_id="close", persistence=dagent.StreamPersistenceOptions())
        while (await anext(stream)).type != "conversation.changed":
            pass
        await asyncio.wait_for(stream.aclose(), 1)
        assert not runner._persistent_deliveries and not runner._active_run_tasks
        assert provider.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", [dagent.ToolAgent, dagent.AutoAgent])
async def test_cancelled_tool_group_is_deactivated_on_next_turn(tmp_path, agent_type):
    started = asyncio.Event()

    @dagent.tool
    async def block() -> str:
        started.set()
        await asyncio.Event().wait()
        return "unobserved"

    provider = MockProvider([*([ChatResponse(content="tool")] if agent_type is dagent.AutoAgent else []),
                             ChatResponse(tool_calls=[ToolCall(id="same", name="tool_block")]), ChatResponse(content="continued")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[], max_retained_runs=0)) as runner:
        host = Host(runner=runner)
        task = asyncio.create_task(invoke(runner, agent_type(profile="conversation", capabilities=[block]),
                                         host, True, input="block", run_id="interrupted"))
        await asyncio.wait_for(started.wait(), 2)
        assert await runner.cancel("interrupted")
        result, _ = await asyncio.wait_for(task, 2)
        assert result.status == "cancelled"
        assistant = next(item for item in host.state.items if isinstance(item, dagent.AssistantMessage))
        observations = [o for b in host.batches for o in b.observations if o.type == "tool_execution"]
        assert observations[-1].status == "outcome_unknown"
        assert observations[-1].assistant_message_id == assistant.id
        result, _ = await invoke(runner, agent_type(profile="conversation", capabilities=[block]), host, True,
                                 input=None, conversation=host.state, run_id="retry")
        assert result.status == "completed"
        assert sum(isinstance(i, dagent.UserMessage) for i in host.state.items) == 1
        assert not any(i.id == assistant.id for i in host.state.items)
        assert any(b.context and assistant.id in b.context.deactivated_item_ids for b in host.batches)
        assert (host.state.id, assistant.id) in host.records
        assert not any(message.get("tool_calls") for message in provider.requests[-1]["messages"])


@pytest.mark.asyncio
async def test_cancel_preserves_partial_response_as_audit_only(tmp_path):
    emitted = asyncio.Event()
    class BlockingProvider(MockProvider):
        async def stream_chat(self, *args, **kwargs):
            yield ChatStreamEvent(type="token", content="observed prefix")
            emitted.set()
            await asyncio.Event().wait()
    with closing(dagent.Runner(provider=BlockingProvider(), workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        task = asyncio.create_task(invoke(runner, dagent.ToolAgent(profile="conversation"), host, True,
                                         input="hi", run_id="cancel"))
        await asyncio.wait_for(emitted.wait(), 2)
        assert await runner.cancel("cancel")
        result, _ = await asyncio.wait_for(task, 2)
        assert result.status == "cancelled"
        assert [r.item.content for b in host.batches for r in b.messages if r.partial] == ["observed prefix"]
        assert all(isinstance(i, dagent.UserMessage) for i in host.state.items)


@pytest.mark.asyncio
async def test_persistence_rejects_dag_but_default_dag_still_works(tmp_path):
    with closing(dagent.Runner(provider=MockProvider(), workspace=tmp_path, skill_roots=[])) as runner:
        with pytest.raises(ValueError, match="ToolLoop"):
            async for _ in runner.stream(dagent.DagAgent(), input="hi", persistence=dagent.StreamPersistenceOptions()):
                pass
        assert "on_conversation_change" not in __import__('inspect').signature(runner.run).parameters
        assert not hasattr(dagent, "ConversationChangeError")


@pytest.mark.asyncio
async def test_terminal_has_only_output_reference_and_no_trace_or_history(tmp_path, monkeypatch):
    from dagent.schemas.run_trace import RunTrace
    monkeypatch.setattr(RunTrace, "__init__", lambda *a, **kw: pytest.fail("trace constructed"))
    with closing(dagent.Runner(provider=MockProvider([ChatResponse(content="answer")]), workspace=tmp_path,
                              skill_roots=[], max_retained_runs=0)) as runner:
        host = Host(runner=runner)
        _, events = await invoke(runner, dagent.ToolAgent(profile="conversation"), host, True, input="hi")
        final = events[-1].data
        assert final.status == "completed" and final.output_item_id and final.output_text is None
        assert final.checkpoint is None
        assert not {"trace.updated", "run.finished", "capability.call.completed"} & {e.type for e in events}
        assert "conversation" not in final.model_dump() and "model_thread" not in final.model_dump()
        assert all(dagent.RunStreamEvent.model_validate(e.model_dump(mode="json")) == e for e in events)
        assert not runner.runtime.runs and not runner._run_checkpoints


@pytest.mark.parametrize("options", [
    {"ack_timeout_seconds": 0}, {"ack_timeout_seconds": float("inf")},
    {"max_buffer_events": 256}, {"max_buffer_bytes": 1_048_576},
    {"stall_timeout_seconds": 1}, {"trace_mode": "off"},
])
def test_persistence_configuration_accepts_only_ack_timeout(options):
    with pytest.raises(ValueError):
        dagent.StreamPersistenceOptions(**options)


@pytest.mark.asyncio
async def test_slow_reader_keeps_all_display_deltas_without_stalling_the_model(tmp_path):
    class TokenProvider(MockProvider):
        emitted = 0
        finished = asyncio.Event()
        async def stream_chat(self, *args, **kwargs):
            for _ in range(100):
                self.emitted += 1
                yield ChatStreamEvent(type="token", content="x" * 128)
            self.finished.set()
            yield ChatStreamEvent(type="done", response=ChatResponse(content="x" * 12800))
    provider = TokenProvider()
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        stream = runner.stream(dagent.ToolAgent(profile="conversation"), input="hi", run_id="slow",
                               persistence=dagent.StreamPersistenceOptions())
        text = []
        async for event in stream:
            if event.type == "conversation.changed":
                await host.save(event.data)
                assert not await runner.acknowledge_changes("other", event.data.batch_id)
                assert await runner.acknowledge_changes(event.run_id, event.data.batch_id)
                assert await runner.acknowledge_changes(event.run_id, event.data.batch_id)
            elif event.type == "response.content.delta":
                text.append(event.data.delta)
                if len(text) == 1:
                    await asyncio.wait_for(provider.finished.wait(), 1)
                    assert provider.emitted == 100
        assert "".join(text) == "x" * 12800
        assert text == ["x" * 128] * 100
        assert host.state.items[-1].content == "x" * 12800


@pytest.mark.asyncio
async def test_result_storage_failure_is_a_public_known_execution_fact(tmp_path, monkeypatch):
    from dagent.capabilities.tools.registry import ToolOutput
    from dagent.harness_runtime import result_storage
    effects = []
    @dagent.tool
    def binary():
        effects.append("once")
        return ToolOutput("produced", value=b"binary")
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(result_storage, "_write_reference", fail)
    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="binary", name="tool_binary")]), ChatResponse(content="must not run")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        result, _ = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[binary]), host, True, input="produce")
        assert result.status == "failed" and result.error.code == "ResultStorageError"
        assert result.checkpoint is None and effects == ["once"] and len(provider.requests) == 1
        tool = [o for b in host.batches for o in b.observations if o.type == "tool_execution"][-1]
        assert tool.status == "completed" and "value" in tool.retention.unavailable_fields
        assert tool.retention.storage_warnings[0].error_type == "OSError"
        assert not any(isinstance(i, dagent.ToolResultMessage) for i in host.state.items)


@pytest.mark.asyncio
async def test_rejected_resource_does_not_adopt_candidate_context(tmp_path):
    initial = dagent.ConversationState(items=(
        dagent.UserMessage(run_id="old", content="collect"),
        dagent.AssistantMessage(run_id="old", tool_calls=(dagent.ToolCallItem(id="old_call", name="query"),)),
        dagent.ToolResultMessage(run_id="old", call_id="old_call", name="query", status="completed",
                                 content=dagent.InlineContent(text="evidence " * 3000)),
        dagent.AssistantMessage(run_id="old", content="collected"),
    ))
    provider = MockProvider()
    provider.configured_context_window_tokens, provider.max_output_tokens = 4096, 512
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(initial, runner)
        original = host.save
        async def save(batch):
            if batch.resources:
                raise OSError("file store unavailable")
            await original(batch)
        host.save = save
        _, events = await consume(runner, runner.stream(dagent.ToolAgent(profile="conversation", capabilities=["tool.read_file"],
            context=dagent.ContextPolicy(compaction_trigger_ratio=.1, compaction_retain_ratio=.05, max_tool_result_tokens=128)),
            input="continue", conversation=initial, persistence=dagent.StreamPersistenceOptions()), host)
        assert events[-1].type == "run.failed" and events[-1].data.error_type == "PersistenceRejected"
        assert any(e.type == "conversation.changed" and e.data.resources for e in events)
        assert provider.requests == [] and host.state.summary is None
        assert all(item in host.state.items for item in initial.items)


@pytest.mark.asyncio
async def test_confirmed_reviews_release_heavy_state_but_keep_replay_ids(tmp_path):
    @dagent.tool(risk="medium")
    def gate(values: list[str]) -> str:
        return "ok"
    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="gate", name="tool_gate", arguments={"values": ["original"]})]) for _ in range(20)])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        for index in range(20):
            host = Host(runner=runner)
            result, _ = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[gate], review="careful"),
                                     host, True, input="hold", run_id=f"pending_{index}")
            assert result.requires_review
            calls = result.checkpoint.pending_tool_invocations
            calls[0].arguments["values"].append("host")
            assert result.checkpoint.pending_tool_invocations[0].arguments == {"values": ["original"]}
            assert not runner.runtime.runs and not runner._run_checkpoints
            assert not runner.runtime.session._review_index
        assert len(runner._used_run_ids) == 20
        with pytest.raises(ValueError, match="already"):
            await runner.run(dagent.ToolAgent(profile="conversation"), input="again", run_id="pending_0")


@pytest.mark.asyncio
async def test_metadata_clearing_and_noncontiguous_summary_replacement():
    from dagent.harness_runtime.conversation_changes import ConversationChangePublisher
    from dagent.harness_runtime.stream_delivery import StreamDelivery
    from dagent.schemas.conversation import CompactionState
    initial = dagent.ConversationState(revision=5, items=tuple(
        dagent.AssistantMessage(id=f"item_{i}", content=f"part {i}") for i in range(5)),
        summary=dagent.ContextSummary(content="previous", source_item_count=2),
        compaction_state=CompactionState(failed_sources=("source",)))
    host = Host(initial)
    publisher = ConversationChangePublisher(StreamDelivery(dagent.StreamPersistenceOptions()), initial)
    publisher.bind("run", "tool", None, ".runtime")
    async def adopt(state):
        task = asyncio.create_task(publisher.publish(state=state))
        event = await publisher.delivery.receive()
        await host.save(event.data)
        publisher.delivery.acknowledge("run", event.data.batch_id)
        await task
    adopted = initial.model_copy(update={"revision": 8, "items": (initial.items[0], initial.items[2], initial.items[4]),
        "summary": dagent.ContextSummary(content="replacement", source_item_count=4), "compaction_state": None})
    await adopt(adopted)
    assert host.state == adopted
    change = host.batches[-1].context
    assert change.summary_change.replaced_item_ids == ("item_1", "item_3")
    assert change.deactivated_item_ids == () and change.compaction_state_change.value is None
    cleared = adopted.model_copy(update={"revision": 9, "summary": None})
    await adopt(cleared)
    assert host.state == cleared and host.batches[-1].context.summary_change.value is None


@pytest.mark.asyncio
async def test_transport_error_keeps_observed_prefix_without_activating_it(tmp_path):
    from dagent.providers.openai_compatible import ProviderRequestError
    class BrokenProvider(MockProvider):
        async def stream_chat(self, *args, **kwargs):
            yield ChatStreamEvent(type="token", content="observed prefix")
            raise ProviderRequestError(OSError("connection closed"),
                                       metadata=dagent.ModelCallMetadata(protocol="chat_completions"))
    with closing(dagent.Runner(provider=BrokenProvider(), workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        result, _ = await invoke(runner, dagent.ToolAgent(profile="conversation"), host, True, input="hello")
        assert result.status == "failed"
        assert all(isinstance(i, dagent.UserMessage) for i in host.state.items)
        responses = [o for b in host.batches for o in b.observations if o.type == "response" and o.status == "failed"]
        assert responses[-1].observed_content == "observed prefix"
        assert responses[-1].error.code == "provider_request_failed"


@pytest.mark.asyncio
async def test_host_resource_save_does_not_exhaust_compaction_timeout(tmp_path, monkeypatch):
    from dagent.harness_runtime.conversation_changes import ConversationChangePublisher, conversation_change_scope, current_changes
    from dagent.harness_runtime.stream_delivery import StreamDelivery
    from tests.test_compaction_strategy import compactor, summary_response
    clock = [0.0]
    monkeypatch.setattr("dagent.harness_runtime.compaction.monotonic", lambda: clock[0])
    async def summarize(request):
        await asyncio.sleep(0)
        return summary_response(request)
    initial = dagent.ConversationState()
    publisher = ConversationChangePublisher(StreamDelivery(dagent.StreamPersistenceOptions()), initial)
    publisher.bind("compaction", "tool", tmp_path, ".runtime")
    async def writer():
        while (event := await publisher.delivery.receive()) is not None:
            assert current_changes() is None
            assert event.data.resources
            clock[0] += 10
            publisher.delivery.acknowledge("compaction", event.data.batch_id)
    task = asyncio.create_task(writer())
    try:
        with conversation_change_scope(publisher):
            engine = compactor(tmp_path, summarize, timeout=1)
            summary = await engine.compact(None, (dagent.AssistantMessage(content="Earlier work completed."),), 256)
        assert summary.method == "model" and engine.deadline == 11
    finally:
        publisher.delivery.finish()
        await task


@pytest.mark.asyncio
async def test_context_retry_does_not_silently_drop_new_uploads(tmp_path):
    provider = MockProvider([ChatResponse(content="must not run")])
    initial = dagent.ConversationState(items=(dagent.UserMessage(content="saved request"),))
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        events = [event async for event in runner.stream(dagent.ToolAgent(profile="conversation"), input=None,
            conversation=initial, input_uploads=[dagent.ArtifactUpload(filename="new.txt", content=b"new")],
            persistence=dagent.StreamPersistenceOptions())]
        assert events[-1].type == "run.failed" and events[-1].data.error_type == "ValueError"
        assert "input_uploads" in events[-1].data.message
        assert provider.requests == []


@pytest.mark.asyncio
async def test_duplicate_auto_route_failure_cannot_replace_or_release_another_delivery(tmp_path, monkeypatch):
    from dagent.harness_runtime.runtime import HarnessRuntime
    gates = [asyncio.Event(), asyncio.Event()]
    arrived, generating = asyncio.Event(), asyncio.Event()
    routes = []
    async def route(self, message):
        index = len(routes)
        routes.append(message)
        if len(routes) == 2:
            arrived.set()
        await gates[index].wait()
        return "tool", None, None
    monkeypatch.setattr(HarnessRuntime, "_route", route)
    class BlockingProvider(MockProvider):
        async def stream_chat(self, *args, **kwargs):
            yield ChatStreamEvent(type="token", content="prefix")
            generating.set()
            await asyncio.Event().wait()
    with closing(dagent.Runner(provider=BlockingProvider(), workspace=tmp_path, skill_roots=[])) as runner:
        agent = dagent.AutoAgent(profile="conversation", capabilities=[])
        host = Host(runner=runner)
        first = asyncio.create_task(invoke(runner, agent, host, True, input="first", run_id="same"))
        second = asyncio.create_task(consume(runner, runner.stream(agent, input="second", run_id="same",
                                            persistence=dagent.StreamPersistenceOptions()), Host(runner=runner)))
        await asyncio.wait_for(arrived.wait(), 2)
        gates[0].set()
        await asyncio.wait_for(generating.wait(), 2)
        owner = runner._persistent_deliveries["same"]
        gates[1].set()
        result, events = await asyncio.wait_for(second, 2)
        assert result is None and events[-1].type == "run.failed"
        assert events[-1].data.error_type == "RuntimeError", events[-1].data.message
        assert runner._persistent_deliveries["same"] is owner
        assert not first.done()
        assert await runner.cancel("same")
        result, _ = await asyncio.wait_for(first, 2)
        assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_premature_review_resume_does_not_consume_the_decision(tmp_path):
    effects = []
    @dagent.tool(risk="medium")
    def gate() -> str:
        effects.append("once")
        return "ok"
    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="gate", name="tool_gate")]), ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        stream = runner.stream(dagent.ToolAgent(profile="conversation", capabilities=[gate], review="careful"),
                               input="review", persistence=dagent.StreamPersistenceOptions())
        async for event in stream:
            if event.type == "conversation.changed":
                await host.save(event.data)
                if event.data.status == "awaiting_review":
                    checkpoint = event.data.checkpoint
                    decision = checkpoint.review.approve()
                    with pytest.raises(ValueError, match="active"):
                        await runner.resume(decision, checkpoint=checkpoint)
                    assert decision.review_id not in runner._consumed_review_ids
                    assert effects == []
                await runner.acknowledge_changes(event.run_id, event.data.batch_id)
        result, _ = await resume_invoke(runner, decision, checkpoint, host)
        assert result.status == "completed" and effects == ["once"]


@pytest.mark.asyncio
async def test_closing_finished_default_stream_cannot_unlink_a_persistent_resume(tmp_path):
    generating = asyncio.Event()
    class BlockingProvider(MockProvider):
        async def stream_chat(self, *args, **kwargs):
            if len(self.requests) > 0:
                yield ChatStreamEvent(type="token", content="prefix")
                generating.set()
                await asyncio.Event().wait()
            else:
                async for event in super().stream_chat(*args, **kwargs):
                    yield event
    @dagent.tool(risk="medium")
    def gate() -> str:
        return "ok"
    with closing(dagent.Runner(provider=BlockingProvider([ChatResponse(tool_calls=[ToolCall(id="gate", name="tool_gate")])]),
                              workspace=tmp_path, skill_roots=[])) as runner:
        old_stream = runner.stream(dagent.ToolAgent(profile="conversation", capabilities=[gate], review="careful"), input="review")
        async for event in old_stream:
            if event.type == "run.finished":
                old = event.data.result
                break
        assert old.checkpoint.state.trace is not None
        host = Host(old.conversation, runner)
        task = asyncio.create_task(resume_invoke(runner, old.review.approve(), old.checkpoint, host))
        await asyncio.wait_for(generating.wait(), 2)
        await old_stream.aclose()
        assert await runner.cancel(old.run_id)
        result, _ = await asyncio.wait_for(task, 2)
        assert result.status == "cancelled"
        assert runner.run_checkpoint(old.run_id) is None


@pytest.mark.asyncio
async def test_runner_close_cannot_leave_a_late_resume_checkpoint_or_state(tmp_path):
    executing = asyncio.Event()
    @dagent.tool(risk="medium")
    async def gate() -> str:
        executing.set()
        await asyncio.Event().wait()
        return "unobserved"
    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="gate", name="tool_gate")])])
    runner = dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])
    try:
        host = Host(runner=runner)
        pending, _ = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[gate], review="careful"),
                                  host, True, input="review")
        task = asyncio.create_task(resume_invoke(runner, pending.review.approve(), pending.checkpoint, host))
        await asyncio.wait_for(executing.wait(), 2)
        runner.close()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        assert not runner.runtime.runs and not runner._run_checkpoints
        assert not runner._active_run_tasks and not runner._persistent_deliveries
    finally:
        runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["run", "stream", "resume", "static_dag", "thread", "persistent_stream"])
async def test_runner_calls_from_a_persistent_tool_are_independent_runs(tmp_path, call):
    holder, nested = {}, {}

    @dagent.tool(risk="medium")
    def gate() -> str:
        return "gated"

    @dagent.tool
    def double(x: int) -> int:
        return x * 2

    async def nested_run(runner):
        agent = dagent.ToolAgent(profile="conversation", capabilities=[gate], review="careful" if call == "resume" else "fast")
        if call == "static_dag":
            dag = dagent.Dag("workflow")
            dag.add_node(dagent.Node("double", target=double, inputs={"x": dag.input["x"]}))
            return await runner.run(dag, graph_input={"x": 3})
        if call == "stream":
            events = [event async for event in runner.stream(agent, input="nested")]
            assert events[-1].type == "run.finished"
            return events[-1].data.result
        if call == "persistent_stream":
            child = Host(runner=runner)
            turn, _ = await consume(runner, runner.stream(agent, input="nested",
                                    persistence=dagent.StreamPersistenceOptions()), child)
            return turn
        result = await runner.run(agent, input="nested")
        if call == "resume":
            assert result.status == "awaiting_review"
            result = await runner.resume(result.review.approve(), checkpoint=result.checkpoint)
        return result

    @dagent.tool
    async def delegate() -> str:
        if call == "thread":
            # A worker thread copies the Run's context into its own event loop.
            nested["result"] = await asyncio.to_thread(lambda: asyncio.run(nested_run(holder["runner"])))
        else:
            nested["result"] = await nested_run(holder["runner"])
        return "delegated"

    responses = [ChatResponse(tool_calls=[ToolCall(id="delegate", name="tool_delegate")])]
    if call != "static_dag":
        responses += [ChatResponse(tool_calls=[ToolCall(id="gate", name="tool_gate")]),
                      ChatResponse(content="nested done")]
    responses.append(ChatResponse(content="parent done"))
    with closing(dagent.Runner(provider=MockProvider(responses), workspace=tmp_path, skill_roots=[],
                               capabilities=[gate, double])) as runner:
        holder["runner"] = runner
        host = Host(runner=runner)
        parent, events = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[delegate]),
                                      host, True, input="delegate")
        assert parent.status == "completed" and parent.output_text == "parent done"
        assert {batch.run_id for batch in host.batches} == {parent.run_id}
        assert {event.run_id for event in events} == {parent.run_id}
        assert runner.run_state(parent.run_id).trace is None
        child = nested["result"]
        assert child.status == "completed" and child.run_id != parent.run_id
        if call == "persistent_stream":
            assert child.trace is None and child.output_text == "nested done"
        elif call == "static_dag":
            assert child.trace is not None and child.checkpoint is not None
            assert child.trace.root.children[-1].value == 6
        else:
            assert child.trace is not None and child.checkpoint is not None
            assert child.new_items and child.output_text == "nested done"


@pytest.mark.asyncio
async def test_capability_test_from_a_persistent_tool_records_nothing_in_that_run(tmp_path):
    holder = {}

    @dagent.tool
    async def probe() -> str:
        result = await holder["runner"].test_capability("agent.helper", {"prompt": "help"})
        return result.content

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="probe", name="tool_probe")]),
                             ChatResponse(content="child answer"), ChatResponse(content="parent done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        holder["runner"] = runner
        runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[]))
        host = Host(runner=runner)
        parent, _ = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[probe]),
                                 host, True, input="probe")
        assert parent.status == "completed" and parent.output_text == "parent done"
        assert [item.content.text for item in host.state.items if item.type == "tool_result"] == ["child answer"]
        assert all(record.origin.scope == "conversation" for batch in host.batches for record in batch.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent_resume", [True, False])
async def test_review_resumes_right_after_its_terminal_batch_is_acknowledged(tmp_path, persistent_resume):
    effects = []

    @dagent.tool(risk="medium")
    def gate() -> str:
        effects.append("once")
        return "ok"

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="gate", name="tool_gate")]), ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        resumed = None
        async for event in runner.stream(dagent.ToolAgent(profile="conversation", capabilities=[gate], review="careful"),
                                         input="review", persistence=dagent.StreamPersistenceOptions()):
            if event.type != "conversation.changed":
                continue
            await host.save(event.data)
            assert await runner.acknowledge_changes(event.run_id, event.data.batch_id)
            if event.data.status == "awaiting_review":
                # The previous delivery is still open here; its producer has
                # only synchronous cleanup left once this ACK is accepted.
                checkpoint = event.data.checkpoint
                if persistent_resume:
                    resumed, _ = await resume_invoke(runner, checkpoint.review.approve(), checkpoint, host)
                else:
                    events = [item async for item in runner.resume_stream(checkpoint.review.approve(), checkpoint=checkpoint)]
                    resumed = events[-1].data.result
        assert resumed.status == "completed" and resumed.output_text == "done" and effects == ["once"]
        assert not runner._active_run_tasks and not runner._persistent_deliveries
