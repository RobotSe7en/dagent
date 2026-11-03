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


async def invoke(runner, agent, host, stream, **kwargs):
    if stream:
        events = [event async for event in runner.stream(agent, on_conversation_change=host.save, **kwargs)]
        assert events[-1].type == "run.finished", events[-1]
        return events[-1].data.result, events
    return await runner.run(agent, on_conversation_change=host.save, **kwargs), []


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
@pytest.mark.parametrize("stream", [False, True])
async def test_review_resume_uses_new_delivery_and_original_run_id(tmp_path, stream):
    executed = []

    @dagent.tool(risk="medium")
    def record() -> str:
        executed.append("effect")
        return "saved"

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="record", name="tool_record", arguments={})]),
        ChatResponse(content="finished"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        first, _ = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[record], review="careful"),
                                host, stream, input="record")
        assert host.state == first.conversation
        assert executed == []
        checkpoint = dagent.RunCheckpoint.model_validate_json(first.checkpoint.model_dump_json())
        if stream:
            events = [event async for event in runner.resume_stream(first.review.approve(), checkpoint=checkpoint,
                                                                    on_conversation_change=host.save)]
            result = events[-1].data.result
        else:
            result = await runner.resume(first.review.approve(), checkpoint=checkpoint, on_conversation_change=host.save)
        assert host.state == result.conversation
        assert executed == ["effect"]
        assert len(host.sequences) == 2
        assert {batch.run_id for batch in host.batches} == {first.run_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_planner_audit_is_not_public_context(tmp_path, stream):
    provider = MockProvider([ChatResponse(content=final_answer_response("answer"))])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        result, events = await invoke(runner, dagent.DagAgent(capabilities=[]), host, stream, input="hello")
        assert result.status == "completed"
        assert host.state == result.conversation
        audit = [record for batch in host.batches for record in batch.messages if record.origin.scope == "planner"]
        assert {item.id for item in result.new_items} <= {key[1] for key in host.records}
        assert audit
        assert all(item.scope == "conversation" for item in host.state.items)
        if events:
            started = next(event for event in events if event.type == "response.started")
            assert any(record.item.id == started.data.message_id for record in audit)


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
@pytest.mark.parametrize("stream", [False, True])
async def test_save_failure_after_effect_stops_next_call_and_preserves_result(tmp_path, stream):
    executed = []

    @dagent.tool
    def record(value: str) -> str:
        executed.append(value)
        return value

    provider = MockProvider([ChatResponse(tool_calls=[
        ToolCall(id="one", name="tool_record", arguments={"value": "one"}),
        ToolCall(id="two", name="tool_record", arguments={"value": "two"}),
    ])])
    saved = []

    async def failing_save(batch):
        saved.append(batch)
        if any(isinstance(record.item, dagent.ToolResultMessage) for record in batch.messages):
            raise OSError("host commit failed")

    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        agent = dagent.ToolAgent(profile="conversation", capabilities=[record])
        if stream:
            events = [event async for event in runner.stream(agent, input="record", on_conversation_change=failing_save)]
            assert [event.type for event in events].count("run.failed") == 1
            assert events[-1].data.error_type == "ConversationChangeError"
            assert events[-1].data.conversation_change_failure.failed_batch == saved[-1]
            result = events[-1].data.result
        else:
            with pytest.raises(dagent.ConversationChangeError) as captured:
                await runner.run(agent, input="record", on_conversation_change=failing_save)
            assert isinstance(captured.value.__cause__, OSError)
            assert captured.value.failed_batch == saved[-1]
            assert captured.value.acknowledged_sequence == saved[-1].sequence - 1
            result = captured.value.result
        assert executed == ["one"]
        assert len(provider.requests) == 1
        assert result.status == "failed"
        assert result.checkpoint is None
        assert runner.run_checkpoint(result.run_id) is None
        assert result.trace.root.children[-1].capability_execution.result.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("fail_at", ["response_start", "result", "validation_start", "validation_result"])
@pytest.mark.parametrize("attempts", [1, 2])
async def test_retry_delivery_failure_preserves_prior_execution_traces(tmp_path, stream, resume, fail_at, attempts):
    executed = []

    @dagent.tool(risk="medium" if resume else "low")
    def prepare() -> str:
        executed.append("prepare")
        return "prepared"

    @dagent.tool
    def retry(value: str) -> str:
        executed.append(value)
        return value

    responses = [ChatResponse(tool_calls=[ToolCall(id="prepare", name="tool_prepare", arguments={})])]
    for attempt in range(attempts):
        responses.extend([
            ChatResponse(content=f"answer {attempt}"),
            ChatResponse(content=json.dumps({"passed": False, "summary": "Try again", "issues": []})),
            ChatResponse(tool_calls=[ToolCall(id=f"retry_{attempt}", name="tool_retry",
                                             arguments={"value": f"retry_{attempt}"})]),
        ])
    if fail_at.startswith("validation_"):
        responses.extend([ChatResponse(content="final answer"),
                          ChatResponse(content=json.dumps({"passed": True, "issues": []}))])
    provider = MockProvider(responses)
    feedback_ids = set()

    async def save(batch):
        feedback_ids.update(record.item.id for record in batch.messages
                            if isinstance(record.item, dagent.UserMessage) and record.item.scope == "validator"
                            and record.origin.scope == "conversation")
        if len(feedback_ids) != attempts:
            return
        if fail_at == "response_start":
            reject = any(obs.type == "response" and obs.status == "started" for obs in batch.observations)
        elif fail_at == "result":
            reject = any(isinstance(record.item, dagent.ToolResultMessage) for record in batch.messages)
        elif fail_at == "validation_start":
            reject = any(obs.type == "response" and obs.status == "started" and obs.origin.scope == "validator"
                         for obs in batch.observations)
        else:
            reject = any(isinstance(record.item, dagent.AssistantMessage) and record.item.scope == "validator"
                         for record in batch.messages)
        if reject:
            raise OSError("retry commit failed")

    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[],
                              validator="validator_agent")) as runner:
        runner.runtime.max_validation_retries = attempts
        agent = dagent.ToolAgent(profile="conversation", capabilities=[prepare, retry],
                                 review="careful" if resume else "fast")
        if resume:
            pending = await runner.run(agent, input="prepare", on_conversation_change=save)
            operation = (runner.resume_stream(pending.review.approve(), checkpoint=pending.checkpoint,
                                               on_conversation_change=save) if stream else
                         runner.resume(pending.review.approve(), checkpoint=pending.checkpoint,
                                       on_conversation_change=save))
        else:
            operation = (runner.stream(agent, input="prepare", on_conversation_change=save) if stream else
                         runner.run(agent, input="prepare", on_conversation_change=save))
        if stream:
            events = [event async for event in operation]
            assert events[-1].type == "run.failed"
            assert events[-1].data.error_type == "ConversationChangeError"
            result = events[-1].data.result
        else:
            with pytest.raises(dagent.ConversationChangeError) as captured:
                await operation
            result = captured.value.result
        expected = ["prepare", *(f"retry_{attempt}" for attempt in range(attempts - (fail_at == "response_start")))]
        assert executed == expected
        executions = [node.capability_execution for node in result.trace.root.children if node.capability_execution]
        assert [execution.invocation.invocation_id for execution in executions] == expected
        assert all(execution.result.status == "completed" for execution in executions)
        assert all(node.parent_id == result.trace.root.id for node in result.trace.root.children)
        assert runner.run_state(result.run_id).trace == result.trace
        assert result.checkpoint is None
        assert len(provider.requests) == len(responses) - (fail_at in {"response_start", "validation_start"})


@pytest.mark.asyncio
async def test_save_backpressure_precedes_model_and_cancellation_waits_for_ack(tmp_path):
    saving = asyncio.Event()
    release = asyncio.Event()
    saved = []
    provider = MockProvider([ChatResponse(content="must not generate")])

    async def save(batch):
        saving.set()
        await release.wait()
        saved.append(batch)

    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(dagent.ToolAgent(profile="conversation"), input="hello",
                                               on_conversation_change=save))
        await saving.wait()
        assert provider.requests == []
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(saved) == 1
        assert provider.requests == []


@pytest.mark.asyncio
async def test_cancelled_stream_keeps_observed_text_without_fabricating_context(tmp_path):
    emitted = asyncio.Event()

    class BlockingProvider(MockProvider):
        async def stream_chat(self, messages, tools=None, *, response_format=None):
            yield ChatStreamEvent(type="token", content="observed prefix")
            emitted.set()
            await asyncio.Event().wait()

    with closing(dagent.Runner(provider=BlockingProvider(), workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        stream = runner.stream(dagent.ToolAgent(profile="conversation"), input="hello", run_id="cancel",
                               on_conversation_change=host.save)
        task = asyncio.create_task(anext(stream))
        await task
        await emitted.wait()
        await runner.cancel("cancel")
        with pytest.raises(asyncio.CancelledError):
            async for _ in stream:
                pass
        partials = [record for batch in host.batches for record in batch.messages if record.partial]
        assert [record.item.content for record in partials] == ["observed prefix"]
        assert all(isinstance(item, dagent.UserMessage) for item in host.state.items)
        assert any(obs.status == "cancelled" for batch in host.batches for obs in batch.observations if obs.type == "response")


@pytest.mark.asyncio
@pytest.mark.parametrize("external", [False, True])
async def test_ready_files_restore_into_another_workspace(tmp_path, external):
    body = "evidence\n" * 3000

    @dagent.tool
    def report() -> str:
        return body

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="report", name="tool_report", arguments={})]),
        ChatResponse(content="saved"), ChatResponse(content="continued"), ChatResponse(content="continued again"),
    ])
    directory = tmp_path / "private" if external else ".runtime"
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, runtime_directory=directory, skill_roots=[],
                              result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024))) as runner:
        host = Host(runner=runner)
        agent = dagent.ToolAgent(profile="conversation", capabilities=[report, "tool.read_file"])
        first, _ = await invoke(runner, agent, host, False, input="report", workspace_path=tmp_path / "first")
        assert host.state == first.conversation
        assert body.encode() in host.resources.values()
        second, _ = await invoke(runner, agent, host, False, input="continue", conversation=host.state,
                                 workspace_path=tmp_path / "second")
        assert host.state == second.conversation
        assert any(workspace == str(tmp_path / "second") for workspace, _ in host.resources)
        third, _ = await invoke(runner, agent, host, False, input="continue again", conversation=host.state,
                                workspace_path=tmp_path / "third")
        assert host.state == third.conversation
        assert any(workspace == str(tmp_path / "third") for workspace, _ in host.resources)


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
async def test_callback_payload_is_detached_from_runtime_state(tmp_path):
    @dagent.tool
    def echo(value: str) -> str:
        assert value == "original"
        return value

    async def mutate(batch):
        for record in batch.messages:
            if isinstance(record.item, dagent.AssistantMessage) and record.item.tool_calls:
                record.item.tool_calls[0].arguments["value"] = "mutated"

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="c", name="tool_echo", arguments={"value": "original"})]),
        ChatResponse(content="done"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[echo]), input="echo",
                                  on_conversation_change=mutate)
        assert result.status == "completed"
        assert result.conversation.items[1].tool_calls[0].arguments == {"value": "original"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_opt_in_does_not_change_provider_requests(tmp_path, stream):
    requests = []
    for enabled in [False, True]:
        provider = MockProvider([ChatResponse(content="answer", reasoning_content="reason")])
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            host = Host(runner=runner)
            if stream:
                events = [event async for event in runner.stream(dagent.ToolAgent(profile="conversation", capabilities=[]),
                    input="question", workspace_path=tmp_path / "session", run_id="wire", on_conversation_change=host.save if enabled else None)]
                if not enabled:
                    assert all("message_id" not in event.data.__dict__ or event.data.message_id is None for event in events)
                    assert all("message_id" not in event.model_dump(mode="json")["data"] for event in events)
            else:
                await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[]), input="question", workspace_path=tmp_path / "session", run_id="wire",
                                 on_conversation_change=host.save if enabled else None)
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


@pytest.mark.asyncio
async def test_metadata_clearing_and_noncontiguous_summary_replacement():
    from dagent.harness_runtime.conversation_changes import ConversationChangePublisher
    from dagent.schemas.conversation import CompactionState

    initial = dagent.ConversationState(revision=5, items=tuple(
        dagent.AssistantMessage(id=f"item_{index}", content=f"part {index}") for index in range(5)
    ), summary=dagent.ContextSummary(content="previous", source_item_count=2),
        compaction_state=CompactionState(failed_sources=("source",)))
    host = Host(initial)
    publisher = ConversationChangePublisher(host.save, initial)
    publisher.bind("run", "tool", None, ".runtime")
    adopted = initial.model_copy(update={
        "revision": 8, "items": (initial.items[0], initial.items[2], initial.items[4]),
        "summary": dagent.ContextSummary(content="replacement", source_item_count=4),
        "compaction_state": None,
    })
    await publisher.publish(state=adopted)
    assert host.state == adopted
    change = host.batches[-1].context
    assert change.summary_change.replaced_item_ids == ("item_1", "item_3")
    assert change.deactivated_item_ids == ()
    assert change.compaction_state_change.value is None
    cleared = adopted.model_copy(update={"revision": 9, "summary": None})
    await publisher.publish(state=cleared)
    assert host.state == cleared
    assert host.batches[-1].context.summary_change.value is None


@pytest.mark.asyncio
async def test_resume_save_failure_is_not_a_replayable_checkpoint(tmp_path):
    executed = []

    @dagent.tool(risk="medium")
    def effect() -> str:
        executed.append("effect")
        return "effect"

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="effect", name="tool_effect", arguments={})])])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        first = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[effect], review="careful"), input="effect")

        async def fail(batch):
            if any(isinstance(record.item, dagent.ToolResultMessage) for record in batch.messages):
                raise OSError("cannot commit")

        with pytest.raises(dagent.ConversationChangeError) as captured:
            await runner.resume(first.review.approve(), checkpoint=first.checkpoint, on_conversation_change=fail)
        assert executed == ["effect"]
        assert len(provider.requests) == 1
        assert captured.value.result.checkpoint is None
        assert runner.run_checkpoint(first.run_id) is None


@pytest.mark.asyncio
async def test_static_agent_review_only_publishes_audit_records(tmp_path):
    @dagent.tool(risk="medium")
    def effect() -> str:
        return "effect"

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="effect", name="tool_effect", arguments={})]),
        ChatResponse(content="child complete"),
    ])
    child = dagent.ToolAgent(name="helper", profile="conversation", capabilities=[effect])
    graph = dagent.Dag("audit")
    graph.add_node(dagent.Node("child", target=child, inputs={"prompt": "do it"}))
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        first = await runner.run(graph, review="careful", on_conversation_change=host.save)
        assert first.status == "awaiting_review"
        events = [event async for event in runner.resume_stream(first.review.approve(), checkpoint=first.checkpoint,
                                                                on_conversation_change=host.save)]
        assert events[-1].type == "run.finished"
        assert events[-1].data.result.status == "completed"
        assert host.state is None
        assert host.batches
        assert all(batch.context is None and batch.conversation_id is None and batch.revision is None for batch in host.batches)
        assert any(isinstance(record.item, dagent.ToolResultMessage) for batch in host.batches for record in batch.messages)


@pytest.mark.asyncio
async def test_validator_audit_and_feedback_preserve_public_state(tmp_path):
    @dagent.tool
    def evidence() -> str:
        return "evidence"

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="evidence", name="tool_evidence", arguments={})]),
        ChatResponse(content="first answer"),
        ChatResponse(content=json.dumps({"passed": False, "issues": [{"message": "explain more"}], "summary": "retry"})),
        ChatResponse(content="improved answer"),
        ChatResponse(content=json.dumps({"passed": True, "issues": [], "summary": "good"})),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[],
                              validator=dagent.AgentProfile(name="test_validator", content="Assess the result."))) as runner:
        host = Host(runner=runner)
        result = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[evidence]), input="explain",
                                  on_conversation_change=host.save)
        assert result.status == "completed"
        assert host.state == result.conversation
        recorded = {key[1] for key in host.records}
        assert {item.id for item in result.new_items} <= recorded
        validators = [record for batch in host.batches for record in batch.messages if record.origin.scope == "validator"]
        assert any(isinstance(record.item, dagent.UserMessage) for record in validators)
        assert sum(isinstance(record.item, dagent.AssistantMessage) for record in validators) == 2


@pytest.mark.asyncio
async def test_cancelled_tool_keeps_unknown_outcome_without_result(tmp_path):
    started = asyncio.Event()

    @dagent.tool
    async def block() -> str:
        started.set()
        await asyncio.Event().wait()
        return "unobserved"

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="block", name="tool_block", arguments={})])])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)

        async def consume():
            async for _ in runner.stream(dagent.ToolAgent(profile="conversation", capabilities=[block]), input="block",
                                          run_id="blocking", on_conversation_change=host.save):
                pass

        task = asyncio.create_task(consume())
        await started.wait()
        assert await runner.cancel("blocking")
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not any(isinstance(item, dagent.ToolResultMessage) for item in host.state.items)
        observations = [obs for batch in host.batches for obs in batch.observations if obs.type == "tool_execution"]
        assert observations[-1].status == "outcome_unknown"


def test_optional_response_identity_preserves_positional_event_constructors():
    from dagent.result import ResponseStartedData, ResponseFinishedData, TextDeltaData
    from dagent.harness_runtime.runtime_events import ResponseStreamContext

    assert ResponseStartedData("response", 3).model_step == 3
    assert ResponseFinishedData("response", 3).model_step == 3
    assert TextDeltaData("body", "response", 3).model_step == 3
    assert ResponseStreamContext("run", "response", 3).model_step == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("dag", [False, True])
async def test_cancellation_while_acknowledging_response_start_closes_observation(tmp_path, dag):
    saving = asyncio.Event()
    release = asyncio.Event()
    provider = MockProvider([ChatResponse(content="not requested")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)

        async def save(batch):
            if any(obs.type == "response" and obs.status == "started" for obs in batch.observations):
                saving.set()
                await release.wait()
            await host.save(batch)

        agent = dagent.DagAgent(capabilities=[]) if dag else dagent.ToolAgent(profile="conversation", capabilities=[])
        task = asyncio.create_task(runner.run(agent, input="hello", on_conversation_change=save))
        await saving.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.requests == []
        responses = [obs for batch in host.batches for obs in batch.observations if obs.type == "response"]
        assert [obs.status for obs in responses] == ["started", "cancelled"]
        assert responses[0].message_id == responses[1].message_id


@pytest.mark.asyncio
async def test_invalid_validator_response_is_preserved_before_json_parsing(tmp_path):
    @dagent.tool
    def evidence() -> str:
        return "evidence"

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="evidence", name="tool_evidence", arguments={})]),
        ChatResponse(content="answer"), ChatResponse(content="invalid validator output"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[],
                              validator=dagent.AgentProfile(name="test_validator", content="Assess the result."))) as runner:
        host = Host(runner=runner)
        result = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[evidence]), input="explain",
                                  on_conversation_change=host.save)
        assert host.state == result.conversation
        assert any(record.item.content == "invalid validator output" for batch in host.batches for record in batch.messages)


@pytest.mark.asyncio
async def test_static_dag_save_failure_retains_actual_tool_execution(tmp_path):
    executed = []

    @dagent.tool
    def first() -> str:
        executed.append("first")
        return "evidence"

    @dagent.tool
    def second() -> str:
        executed.append("second")
        return "later"

    graph = dagent.Dag("effects")
    one = graph.add_node(dagent.Node("first", target=first))
    two = graph.add_node(dagent.Node("second", target=second))
    graph.add_edge(one, two)

    async def fail(batch):
        if any(isinstance(record.item, dagent.ToolResultMessage) for record in batch.messages):
            raise OSError("commit failed")

    with closing(dagent.Runner(provider=MockProvider(), workspace=tmp_path, skill_roots=[])) as runner:
        with pytest.raises(dagent.ConversationChangeError) as captured:
            await runner.run(graph, on_conversation_change=fail)
        assert executed == ["first"]
        result = captured.value.result
        assert result.conversation is None
        assert result.checkpoint is None
        assert any(node.capability_execution.result.status == "completed"
                   for node in result.trace.root.children[0].children if node.capability_execution is not None)


@pytest.mark.asyncio
@pytest.mark.parametrize("mapped", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reject_terminal", [False, True])
@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_dag_storage_failure_publishes_known_tool_outcome(tmp_path, monkeypatch, mapped, stream,
                                                              reject_terminal, outcome):
    from dagent.capabilities.tools.registry import ToolOutput
    from dagent.harness_runtime import result_storage

    executed = []

    @dagent.tool
    def binary():
        executed.append("binary")
        return ToolOutput("produced", value=b"\xff")

    if outcome == "failed":
        async def failed_binary(invocation):
            executed.append("binary")
            return dagent.CapabilityResult.failed(invocation, "tool failed", stop_reason="ToolError", value=b"\xff")

        binary = dagent.CapabilityBinding(binary.definition, failed_binary)

    def fail_storage(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(result_storage, "_write_reference", fail_storage)
    graph = dagent.Dag("storage_failure")
    graph.add_node(dagent.MapNode("produce", target=binary, over=[1]) if mapped else
                   dagent.Node("produce", target=binary))
    batches = []

    async def save(batch):
        batches.append(batch)
        if reject_terminal and any(obs.type == "tool_execution" and obs.status == outcome
                                   for obs in batch.observations):
            raise OSError("terminal commit failed")

    with closing(dagent.Runner(provider=MockProvider(), workspace=tmp_path, skill_roots=[])) as runner:
        if stream:
            events = [event async for event in runner.stream(graph, on_conversation_change=save)]
            assert events[-1].type == ("run.failed" if reject_terminal else "run.finished")
            result = events[-1].data.result
            if reject_terminal:
                assert events[-1].data.error_type == "ConversationChangeError"
        elif reject_terminal:
            with pytest.raises(dagent.RunExecutionError) as captured:
                await runner.run(graph, on_conversation_change=save)
            assert isinstance(captured.value, dagent.ConversationChangeError)
            result = captured.value.result
        else:
            result = await runner.run(graph, on_conversation_change=save)
        assert result.status == "failed"
        assert executed == ["binary"]
        observations = [obs for batch in batches for obs in batch.observations if obs.type == "tool_execution"]
        assert [obs.status for obs in observations] == ["starting", outcome]
        assert observations[0].call_id == observations[1].call_id
        assert observations[0].origin == observations[1].origin
        assert observations[1].origin.scope == "planner"
        assert not any(isinstance(record.item, dagent.ToolResultMessage) for batch in batches for record in batch.messages)
        execution = result.trace.dag_node_traces()["produce"].children[0].capability_execution
        assert execution.result.status == outcome
        assert execution.result.retention.unavailable_fields == ("value",)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_map_publishes_each_inline_result(tmp_path, stream):
    @dagent.tool
    def echo(value: str) -> str:
        return value

    graph = dagent.Dag("mapped")
    graph.add_node(dagent.MapNode("each", target=echo, over=["one", "two"], inputs={"value": dagent.item}))
    with closing(dagent.Runner(provider=MockProvider(), workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        result, _ = await invoke(runner, graph, host, stream)
        assert result.node_value("each") == ["one", "two"]
        records = [record for batch in host.batches for record in batch.messages
                   if isinstance(record.item, dagent.ToolResultMessage)]
        assert {record.item.content.text for record in records} == {"one", "two"}
        assert len({record.origin.id for record in records}) == 2
        assert all(record.origin.scope == "planner" for record in records)
        observations = [obs for batch in host.batches for obs in batch.observations if obs.type == "tool_execution"]
        for record in records:
            assert [obs.status for obs in observations if obs.call_id == record.item.call_id] == ["starting", "completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", ["starting", "result"])
async def test_map_save_acknowledgment_gates_dispatch_and_preserves_effects(tmp_path, fail_at):
    executed = []
    saving = asyncio.Event()
    release = asyncio.Event()

    @dagent.tool
    def effect(value: str) -> str:
        executed.append(value)
        return value

    graph = dagent.Dag("mapped")
    graph.add_node(dagent.MapNode("each", target=effect, over=["one", "two"],
                                 inputs={"value": dagent.item}, max_concurrency=1))

    async def save(batch):
        reject = (any(obs.type == "tool_execution" and obs.status == "starting" for obs in batch.observations)
                  if fail_at == "starting" else
                  any(isinstance(record.item, dagent.ToolResultMessage) for record in batch.messages))
        if reject:
            saving.set()
            await release.wait()
            raise OSError("commit failed")

    with closing(dagent.Runner(provider=MockProvider(), workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(graph, on_conversation_change=save))
        try:
            await asyncio.wait_for(saving.wait(), timeout=5)
            assert executed == ([] if fail_at == "starting" else ["one"])
        finally:
            release.set()
        with pytest.raises(dagent.ConversationChangeError) as captured:
            await task
        assert isinstance(captured.value.__cause__, OSError)
        assert executed == ([] if fail_at == "starting" else ["one"])
        children = captured.value.result.trace.dag_node_traces()["each"].children
        assert [child.capability_execution.result.content for child in children] == executed
        assert all(child.status == "completed" for child in children)


@pytest.mark.asyncio
async def test_map_save_failure_retains_all_inflight_effects(tmp_path):
    executed = []
    both_started = asyncio.Event()

    @dagent.tool
    async def effect(value: str) -> str:
        executed.append(value)
        if len(executed) == 2:
            both_started.set()
        await both_started.wait()
        return value

    graph = dagent.Dag("concurrent")
    graph.add_node(dagent.MapNode("each", target=effect, over=["one", "two"],
                                 inputs={"value": dagent.item}, max_concurrency=2))

    async def save(batch):
        if any(isinstance(record.item, dagent.ToolResultMessage) for record in batch.messages):
            raise OSError("commit failed")

    with closing(dagent.Runner(provider=MockProvider(), workspace=tmp_path, skill_roots=[])) as runner:
        with pytest.raises(dagent.ConversationChangeError) as captured:
            await asyncio.wait_for(runner.run(graph, on_conversation_change=save), timeout=5)
        assert set(executed) == {"one", "two"}
        children = captured.value.result.trace.dag_node_traces()["each"].children
        assert [child.capability_execution.result.content for child in children] == ["one", "two"]
        assert all(child.status == "completed" for child in children)


@pytest.mark.asyncio
async def test_nested_argument_mutation_is_replayed_and_continued(tmp_path):
    @dagent.tool
    def append(values: list) -> str:
        values.append("added")
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
        assert call.arguments == {"values": ["original", "added"]}
        restored = dagent.ConversationState.model_validate_json(host.state.model_dump_json())
        await invoke(runner, agent, host, False, input="continue", conversation=restored)
        first_call = next(message for message in provider.requests[1]["messages"] if message.get("tool_calls"))
        restored_call = next(message for message in provider.requests[2]["messages"] if message.get("tool_calls"))
        assert first_call == restored_call


@pytest.mark.asyncio
@pytest.mark.parametrize("bind_initial", [False, True])
async def test_initial_publisher_snapshot_is_detached(bind_initial):
    from dagent.harness_runtime.conversation_changes import ConversationChangePublisher

    initial = dagent.ConversationState(items=(dagent.AssistantMessage(tool_calls=(
        dagent.ToolCallItem(id="call", name="append", arguments={"values": ["original"]}),
    )),))
    host = Host(initial.model_copy(deep=True))
    publisher = ConversationChangePublisher(host.save, None if bind_initial else initial)
    publisher.bind("run", "tool", None, ".runtime", initial=initial if bind_initial else None)
    initial.items[0].tool_calls[0].arguments["values"].append("added")
    await publisher.publish(state=initial.model_copy(update={"revision": 1}))
    assert host.state.items == initial.items
    assert host.batches[-1].context.updated_item_ids == (initial.items[0].id,)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper,fail_at", [
    ("subgraph", "result"), ("loop", "result"), ("agent", "result"),
    ("delegated", "result"), ("delegated", "next_response"),
])
async def test_nested_save_failure_retains_completed_child_trace(tmp_path, wrapper, fail_at):
    executed = []

    @dagent.tool
    def effect() -> str:
        executed.append("effect")
        return "evidence"

    graph = dagent.Dag("outer")
    responses = [ChatResponse(tool_calls=[ToolCall(id="effect", name="tool_effect", arguments={})])]
    if wrapper == "delegated":
        responses.insert(0, ChatResponse(tool_calls=[
            ToolCall(id="delegate", name="agent_helper", arguments={"prompt": "do it"}),
        ]))
    provider = MockProvider(responses)
    if wrapper in {"agent", "delegated"}:
        child = dagent.ToolAgent(name="helper", profile="conversation", capabilities=[effect])
        node = dagent.Node("child", target=child, inputs={"prompt": "do it"})
    else:
        child = dagent.Dag("inner")
        child.add_node(dagent.Node("effect", target=effect))
        node = (dagent.LoopNode("child", body=child, until=dagent.item == "done", max_iterations=2)
                if wrapper == "loop" else dagent.Node("child", target=child))
    graph.add_node(node)

    async def save(batch):
        reject = (any(isinstance(record.item, dagent.ToolResultMessage) for record in batch.messages)
                  if fail_at == "result" else executed and
                  any(obs.type == "response" and obs.status == "started" for obs in batch.observations))
        if reject:
            raise OSError("commit failed")

    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        if wrapper == "delegated":
            runner.add_agent(child)
            target = dagent.ToolAgent(profile="conversation", capabilities=[], agents=["agent.helper"])
        else:
            target = graph
        with pytest.raises(dagent.ConversationChangeError) as captured:
            await runner.run(target, input="do it" if wrapper == "delegated" else None, on_conversation_change=save)
        assert executed == ["effect"]
        trace = captured.value.result.trace
        if wrapper != "delegated":
            assert trace.dag_node_traces()["child"].children

        def effects(node):
            execution = node.capability_execution
            if execution is not None and execution.invocation.capability_id == "tool.effect":
                yield execution.result
            for child_node in node.children:
                assert child_node.parent_id == node.id
                yield from effects(child_node)

        assert [result.status for result in effects(trace.root)] == ["completed"]
        assert captured.value.result.checkpoint is None


@pytest.mark.asyncio
async def test_file_save_failure_does_not_commit_candidate_context_or_call_model(tmp_path):
    initial = dagent.ConversationState(items=(
        dagent.UserMessage(run_id="old", content="collect"),
        dagent.AssistantMessage(run_id="old", tool_calls=(dagent.ToolCallItem(id="old_call", name="query"),)),
        dagent.ToolResultMessage(run_id="old", call_id="old_call", name="query", status="completed",
                                 content=dagent.InlineContent(text="evidence " * 3000)),
        dagent.AssistantMessage(run_id="old", content="collected"),
    ))
    provider = MockProvider()
    provider.configured_context_window_tokens = 4096
    provider.max_output_tokens = 512
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(initial, runner)

        async def save(batch):
            if batch.resources:
                raise OSError("file store unavailable")
            await host.save(batch)

        agent = dagent.ToolAgent(profile="conversation", capabilities=["tool.read_file"], context=dagent.ContextPolicy(
            compaction_trigger_ratio=.1, compaction_retain_ratio=.05, max_tool_result_tokens=128,
        ))
        with pytest.raises(dagent.ConversationChangeError) as captured:
            await runner.run(agent, input="continue", conversation=initial, on_conversation_change=save)
        assert captured.value.failed_batch.resources
        assert provider.requests == []
        assert captured.value.result.conversation == host.state
        assert host.state.summary is None


@pytest.mark.asyncio
async def test_failed_model_response_replays_the_sdk_adopted_failure_state(tmp_path):
    provider = MockProvider([ChatResponse(content="partial", status="failed")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        result = await runner.run(dagent.ToolAgent(profile="conversation"), input="hello", on_conversation_change=host.save)
        assert result.status == "failed"
        assert host.state == result.conversation
        assert any(record.partial and record.item.content == "partial" for batch in host.batches for record in batch.messages)
        assert host.batches[-1].observations[-1].status == "failed"


@pytest.mark.asyncio
async def test_host_callback_does_not_inherit_runtime_publication_scope(tmp_path):
    from dagent.harness_runtime.conversation_changes import current_changes

    async def save(batch):
        # A host may use SDK helpers itself; it must not recursively publish
        # into the run whose acknowledgment lock it currently holds.
        assert current_changes() is None

    with closing(dagent.Runner(provider=MockProvider([ChatResponse(content="answer")]), workspace=tmp_path,
                              skill_roots=[])) as runner:
        result = await runner.run(dagent.ToolAgent(profile="conversation"), input="hello", on_conversation_change=save)
        assert result.status == "completed"


@pytest.mark.asyncio
async def test_transport_error_keeps_observed_prefix_when_sdk_failure_message_is_empty(tmp_path):
    from dagent.providers.openai_compatible import ProviderRequestError

    class BrokenProvider(MockProvider):
        async def stream_chat(self, messages, tools=None, *, response_format=None):
            yield ChatStreamEvent(type="token", content="observed prefix")
            raise ProviderRequestError(OSError("connection closed"),
                                       metadata=dagent.ModelCallMetadata(protocol="chat_completions"))

    with closing(dagent.Runner(provider=BrokenProvider(), workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        events = [event async for event in runner.stream(dagent.ToolAgent(profile="conversation"), input="hello",
                                                         on_conversation_change=host.save)]
        result = events[-1].data.result
        assert result.status == "failed"
        assert host.state == result.conversation
        assert result.conversation.items[-1].content == ""  # Released failure behavior.
        terminal = [obs for batch in host.batches for obs in batch.observations
                    if obs.type == "response" and obs.status == "failed"]
        assert terminal[-1].observed_content == "observed prefix"


@pytest.mark.asyncio
async def test_host_resource_save_does_not_exhaust_compaction_timeout(tmp_path, monkeypatch):
    from dagent.harness_runtime.conversation_changes import ConversationChangePublisher, conversation_change_scope
    from tests.test_compaction_strategy import compactor, summary_response

    clock = [0.0]
    monkeypatch.setattr("dagent.harness_runtime.compaction.monotonic", lambda: clock[0])

    async def save(batch):
        assert batch.resources
        clock[0] += 10  # A slow host exceeds the one-second model allowance.

    async def summarize(request):
        await asyncio.sleep(0)
        return summary_response(request)

    initial = dagent.ConversationState()
    publisher = ConversationChangePublisher(save, initial)
    publisher.bind("compaction", "tool", tmp_path, ".runtime")
    with conversation_change_scope(publisher):
        engine = compactor(tmp_path, summarize, timeout=1)
        summary = await engine.compact(None, (dagent.AssistantMessage(content="Earlier work completed."),), 256)
    assert summary.method == "model"
    assert engine.deadline == 11
