"""Continuation, delivery ownership and late acknowledgement regressions."""

import asyncio
from contextlib import aclosing, closing
import json
import hashlib

import pytest

import dagent
from dagent.harness_runtime.stream_delivery import PersistenceDeliveryError, StreamDelivery
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas.conversation_changes import ConversationChangeBatch
from tests.test_conversation_changes import Host, invoke, resume_invoke


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent_resume", [False, True])
@pytest.mark.parametrize("close_old_reader", [False, True])
async def test_old_persistent_reader_cannot_control_a_resume(tmp_path, persistent_resume, close_old_reader):
    started, release = asyncio.Event(), asyncio.Event()

    @dagent.tool(risk="medium")
    async def gate() -> str:
        started.set()
        await release.wait()
        return "done"

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="gate", name="tool_gate")]),
                             ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        async with aclosing(runner.stream(dagent.ToolAgent(profile="conversation", capabilities=[gate], review="careful"),
                input="gate", persistence=dagent.StreamPersistenceOptions())) as old:
            async for event in old:
                if event.type != "conversation.changed":
                    continue
                await host.save(event.data)
                assert await runner.acknowledge_changes(event.run_id, event.data.batch_id)
                if event.data.status == "awaiting_review":
                    saved = event.data.checkpoint
                    break

            async def resume():
                if persistent_resume:
                    return await resume_invoke(runner, saved.review.approve(), saved, host)
                return [event async for event in runner.resume_stream(saved.review.approve(), checkpoint=saved)]

            resumed = asyncio.create_task(resume())
            try:
                await asyncio.wait_for(started.wait(), 2)
                if close_old_reader:
                    await old.aclose()
                with pytest.raises(ValueError, match="active"):
                    await asyncio.wait_for(runner.resume(saved.review.approve(), checkpoint=saved), 1)
                assert await asyncio.wait_for(runner.cancel(saved.state.run_id), 1)
                if persistent_resume:
                    result, _ = await asyncio.wait_for(resumed, 2)
                    assert result.status == "cancelled"
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(resumed, 2)
                assert len(provider.requests) == 1
            finally:
                release.set()
                if not resumed.done():
                    resumed.cancel()
                await asyncio.gather(resumed, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_ack_at_timeout_does_not_confirm_the_batch(terminal):
    value = StreamDelivery(dagent.StreamPersistenceOptions(ack_timeout_seconds=.001))
    value.run_id = "run"
    batch = ConversationChangeBatch(
        schema_version=2, conversation_schema_version=None, batch_id="delivery:1",
        delivery_id="delivery", sequence=1, conversation_id=None, run_id="run",
        base_revision=None, revision=None,
        **({"status": "completed", "usage": dagent.ExecutionUsage()} if terminal else {}),
    )
    publication = asyncio.create_task(value.publish(batch))
    await value.receive()
    answers = []

    def late_ack(future):
        # wait_for cancels the ACK future before publish records its timeout.
        if future.cancelled():
            answers.append(value.acknowledge("run", "delivery:1"))

    value._ack.add_done_callback(late_ack)
    try:
        with pytest.raises(PersistenceDeliveryError) as raised:
            await asyncio.wait_for(publication, 1)
        assert raised.value.code == "PersistenceTimeout"
        assert answers == [False]
        assert value.acknowledged_sequence == 0
        assert not value.terminal_acknowledged
    finally:
        value.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retries", [0, 1])
@pytest.mark.parametrize("next_input", [None, "continue"])
async def test_final_failed_text_is_replayable_but_discarded_attempts_are_not(tmp_path, retries, next_input):
    usage = dagent.ModelTokenUsage(input_tokens=3, output_tokens=2, total_tokens=5)
    responses = ([ChatResponse(content="discarded", status="incomplete")] if retries else [])
    responses += [ChatResponse(content="partial answer", reasoning_content="reasoning", status="incomplete", usage=usage),
                  ChatResponse(content="continued")]
    provider = MockProvider(responses)
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        agent = dagent.ToolAgent(profile="conversation", capabilities=[], response_retries=retries)
        failed, _ = await invoke(runner, agent, host, True, input="write")
        assert failed.status == "failed"
        assistants = [item for item in host.state.items if item.type == "assistant"]
        assert len(assistants) == 1
        assert assistants[0].content == "partial answer"
        assert assistants[0].reasoning == "reasoning" and assistants[0].usage == usage
        assert any(record.partial and record.item.id == assistants[0].id
                   for batch in host.batches for record in batch.messages)
        saved = dagent.ConversationState.model_validate_json(host.state.model_dump_json())
        continued, _ = await invoke(runner, agent, host, True, input=next_input, conversation=saved)
        assert continued.status == "completed"
        messages = provider.requests[-1]["messages"]
        assert any(item["role"] == "assistant" and item["content"] == "partial answer" for item in messages)
        assert all(item.get("content") != "discarded" for item in messages)
        assert sum(item["role"] == "user" for item in messages) == (1 if next_input is None else 2)


@pytest.mark.asyncio
async def test_failed_call_with_text_stays_audit_only(tmp_path):
    effects = []

    @dagent.tool
    def never() -> str:
        effects.append("unexpected")
        return "no"

    provider = MockProvider([ChatResponse(content="partial with call", status="incomplete",
                                         tool_calls=[ToolCall(id="call", name="tool_never")])])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        result, _ = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[never]), host, True, input="work")
        assert result.status == "failed" and effects == []
        assert [item.type for item in host.state.items] == ["user"]
        assert any(record.item.type == "assistant" and record.item.tool_calls
                   for batch in host.batches for record in batch.messages)


@pytest.mark.asyncio
async def test_persistent_child_failure_keeps_partial_text_for_parent(tmp_path):
    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="child", name="agent_helper", arguments={"prompt": "write"})]),
                             ChatResponse(content="child partial", status="incomplete"), ChatResponse(content="parent done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[]))
        host = Host(runner=runner)
        result, _ = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[], agents=["agent.helper"]),
                                 host, True, input="delegate")
        assert result.status == "completed"
        returned = [message["content"] for message in provider.requests[-1]["messages"] if message["role"] == "tool"]
        assert len(returned) == 1 and "child partial" in returned[0]


@pytest.mark.asyncio
async def test_steer_supersedes_retry_hint_without_erasing_its_audit(tmp_path):
    @dagent.tool
    def echo() -> str:
        return "evidence"

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="echo", name="tool_echo")]),
                             ChatResponse(), ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)
        hints = []
        async with aclosing(runner.stream(dagent.ToolAgent(profile="conversation", capabilities=[echo], response_retries=1),
                input="work", persistence=dagent.StreamPersistenceOptions())) as stream:
            async for event in stream:
                if event.type != "conversation.changed":
                    continue
                await host.save(event.data)
                for record in event.data.messages:
                    if getattr(record.item, "purpose", None) == "response_retry":
                        hints.append(record.item.id)
                        await runner.steer(event.run_id, "additional instruction")
                assert await runner.acknowledge_changes(event.run_id, event.data.batch_id)
        assert len(hints) == 1
        assert (host.state.id, hints[0]) in host.records
        assert hints[0] not in {item.id for item in host.state.items}
        assert any(batch.context and hints[0] in batch.context.deactivated_item_ids for batch in host.batches)
        messages = provider.requests[-1]["messages"]
        assert [message["role"] for message in messages][-2:] == ["tool", "user"]
        assert messages[-1]["content"] == "additional instruction"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["model", "terminal"])
@pytest.mark.parametrize("cancel_reader", [False, True])
async def test_runner_close_ends_delivery_but_preserves_reader_cancellation(tmp_path, stage, cancel_reader):
    from dagent.providers import ChatStreamEvent
    reached, release = asyncio.Event(), asyncio.Event()

    class BlockingProvider(MockProvider):
        async def stream_chat(self, *args, **kwargs):
            yield ChatStreamEvent(type="token", content="prefix")
            reached.set()
            await asyncio.Event().wait()

    provider = BlockingProvider() if stage == "model" else MockProvider([ChatResponse(content="done")])
    runner = dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])
    terminals = []

    async def read():
        async with aclosing(runner.stream(dagent.ToolAgent(profile="conversation", capabilities=[]), input="work",
                persistence=dagent.StreamPersistenceOptions())) as stream:
            async for event in stream:
                if event.type == "conversation.changed":
                    if event.data.status is not None:
                        terminals.append(event.data)
                        reached.set()
                        await release.wait()
                    else:
                        assert await runner.acknowledge_changes(event.run_id, event.data.batch_id)
                assert event.type != "run.failed"

    reader = asyncio.create_task(read())
    try:
        await asyncio.wait_for(reached.wait(), 2)
        runner.close()
        if cancel_reader:
            reader.cancel()
        release.set()
        if cancel_reader:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(reader, 2)
        else:
            await asyncio.wait_for(reader, 2)
        assert len(terminals) == (0 if stage == "model" else 1)
    finally:
        runner.close()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.asyncio
async def test_design_dag_has_no_outer_persistence_scope(tmp_path, monkeypatch):
    from dagent.harness_runtime.context import ContextAssembler
    from dagent.harness_runtime.conversation_changes import current_changes
    original = ContextAssembler.prepare
    scopes = []
    holder = {}

    async def prepare(self, **kwargs):
        response_format = kwargs.get("response_format")
        if response_format is not None and response_format.name == "dagent_dag_design_response":
            scopes.append(current_changes())
        return await original(self, **kwargs)

    monkeypatch.setattr(ContextAssembler, "prepare", prepare)

    @dagent.tool
    async def design() -> str:
        result = await holder["runner"].design_dag("explain the plan")
        return result.type

    answer = ChatResponse(content=json.dumps({"action": "final_answer", "candidate_json": None,
                                             "answer": "independent design", "summary": None}))
    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="design", name="tool_design")]),
                             answer, ChatResponse(content="parent done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        holder["runner"] = runner
        host = Host(runner=runner)
        result, _ = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[design]), host, True,
                                 input="design", run_id="parent")
        assert result.status == "completed" and scopes == [None]
        assert {batch.run_id for batch in host.batches} == {"parent"}


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["immutable", "unversioned", "changed"])
async def test_only_identical_versioned_resources_are_deduplicated(tmp_path, identity):
    work = tmp_path / "work"
    path = work / "runtime" / "results" / "shared.txt"
    path.parent.mkdir(parents=True)
    calls = 0

    def reuse(invocation):
        nonlocal calls
        calls += 1
        body = b"changed" if identity == "changed" and calls == 2 else b"same"
        path.write_bytes(body)
        reference = dagent.ContentReference(path="runtime/results/shared.txt", byte_length=len(body),
            sha256=None if identity == "unversioned" else hashlib.sha256(body).hexdigest())
        return dagent.CapabilityResult.completed(invocation, "result", content_reference=reference)

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="one", name="tool_reuse")]),
                             ChatResponse(tool_calls=[ToolCall(id="two", name="tool_reuse")]), ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, runtime_directory="runtime", skill_roots=[])) as runner:
        runner.register_capability(dagent.CapabilityDefinition(id="tool.reuse", kind="tool", parameters={
            "type": "object", "properties": {},
        }), reuse)
        host = Host(runner=runner)
        result, _ = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=["tool.reuse", "tool.read_file"]),
                                 host, True, input="reuse", workspace_path=work)
        assert result.status == "completed" and calls == 2
        resources = [resource for batch in host.batches for resource in batch.resources]
        assert len(resources) == (1 if identity == "immutable" else 2)


@pytest.mark.asyncio
async def test_resource_cache_evicts_and_does_not_cross_deliveries(tmp_path, monkeypatch):
    from dagent.harness_runtime import conversation_changes
    monkeypatch.setattr(conversation_changes, "_RESOURCE_CACHE_LIMIT", 1)
    work = tmp_path / "work"
    root = work / "runtime" / "results"
    root.mkdir(parents=True)
    names = iter(["a", "b", "a", "a"])

    def reuse(invocation):
        name = next(names)
        body = name.encode()
        (root / name).write_bytes(body)
        reference = dagent.ContentReference(path=f"runtime/results/{name}", byte_length=len(body),
                                             sha256=hashlib.sha256(body).hexdigest())
        return dagent.CapabilityResult.completed(invocation, name, content_reference=reference)

    provider = MockProvider([*[ChatResponse(tool_calls=[ToolCall(id=f"call{i}", name="tool_reuse")]) for i in range(3)],
                             ChatResponse(content="done"), ChatResponse(tool_calls=[ToolCall(id="again", name="tool_reuse")]),
                             ChatResponse(content="done again")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, runtime_directory="runtime", skill_roots=[])) as runner:
        runner.register_capability(dagent.CapabilityDefinition(id="tool.reuse", kind="tool", parameters={
            "type": "object", "properties": {},
        }), reuse)
        host = Host(runner=runner)
        agent = dagent.ToolAgent(profile="conversation", capabilities=["tool.reuse", "tool.read_file"])
        first, _ = await invoke(runner, agent, host, True, input="reuse", workspace_path=work)
        assert sum(len(batch.resources) for batch in host.batches) == 3
        before = len(host.batches)
        second, _ = await invoke(runner, agent, host, True, input="again", conversation=first.conversation, workspace_path=work)
        assert second.status == "completed"
        assert sum(len(batch.resources) for batch in host.batches[before:]) == 1
