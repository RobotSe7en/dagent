"""Public in-process user interaction contracts."""

import asyncio
from contextlib import closing

import pytest
from pydantic import ValidationError

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


def answer(request, value=None, *, abandon=False, **updates):
    identity = dict(run_id=request.run_id, invocation_id=request.invocation_id,
                    interaction_id=request.interaction_id)
    identity.update(updates)
    return (dagent.UserInteractionAbandonment(**identity) if abandon
            else dagent.UserInteractionAnswer(**identity, value=value))


def agent_for(*capabilities):
    return dagent.ToolAgent(profile="conversation", capabilities=list(capabilities))


def provider_for(name="tool_ui_input", arguments=None, call_id="call"):
    return MockProvider([
        ChatResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments if arguments is not None else {"title": "Question"})]),
        ChatResponse(content="done"),
    ])


async def receive(queue):
    return await asyncio.wait_for(queue.get(), 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,arguments,value", [
    ("confirm", {"title": "Continue?", "description": "Details"}, False),
    ("select", {"title": "Choose", "options": [{"id": "stable", "label": "Choice"}]}, "stable"),
    ("input", {"title": "Name", "prompt": "Name please", "initial": "old"}, ""),
    ("editor", {"title": "Edit", "content": "<p>old</p>", "format": "html"}, "<p>new</p>"),
])
@pytest.mark.parametrize("stream", [False, True])
async def test_builtin_values_and_original_call(tmp_path, kind, arguments, value, stream):
    provider = provider_for(f"tool_ui_{kind}", arguments)
    queue, ended, events = asyncio.Queue(), [], []
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        options = dagent.UserInteractionOptions(queue.put_nowait, ended.append)
        agent = agent_for(*dagent.ui_tools())

        async def execute():
            if not stream:
                return await runner.run(agent, input="ask", interactions=options)
            async for event in runner.stream(agent, input="ask", interactions=options):
                events.append(event)
            return events[-1].data.result

        task = asyncio.create_task(execute())
        request = await receive(queue)
        assert len(provider.requests) == 1 and not task.done()
        assert request.protocol_version == 1 and request.type == kind
        assert request.invocation_id == "call" and request.assistant_message_id
        await runner.respond_to_interaction(answer(request, value))
        result = await asyncio.wait_for(task, 5)
        assert result.status == "completed" and len(provider.requests) == 2
        assert len(ended) == 1 and ended[0].reason == "submitted" and not ended[0].invalidated
        item = next(item for item in result.conversation.items if isinstance(item, dagent.ToolResultMessage))
        assert item.call_id == request.invocation_id and item.value == value
        assert provider.requests[-1]["messages"][-1]["role"] == "tool"
        if stream:
            interactions = [event for event in events if event.type.startswith("user_interaction.")]
            assert [event.type for event in interactions] == ["user_interaction.requested", "user_interaction.ended"]
            for event in interactions:
                assert dagent.RunStreamEvent.model_validate(event.model_dump(mode="json")) == event
        with pytest.raises(dagent.UserInteractionResponseRejected):
            await runner.respond_to_interaction(answer(request, value))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,arguments", [
    ("confirm", {"title": "Confirm"}),
    ("select", {"title": "Select", "options": [{"id": "x", "label": "X"}]}),
    ("input", {"title": "Input"}),
    ("editor", {"title": "Editor", "content": "draft"}),
])
async def test_abandonment_is_a_completed_cancel_result(tmp_path, kind, arguments):
    queue = asyncio.Queue()
    provider = provider_for(f"tool_ui_{kind}", arguments)
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(agent_for(*dagent.ui_tools()), input="ask",
                                 interactions=dagent.UserInteractionOptions(queue.put_nowait)))
        request = await receive(queue)
        await runner.respond_to_interaction(answer(request, abandon=True))
        result = await asyncio.wait_for(task, 5)
        item = next(item for item in result.conversation.items if isinstance(item, dagent.ToolResultMessage))
        assert result.status == "completed" and item.status == "completed"
        assert item.value == {"status": "cancelled"}
        assert "USER_INTERACTION_CANCELLED" in provider.requests[-1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_custom_context_continues_locals_and_rejects_escaped_handle(tmp_path):
    calls, handles, queue = [], [], asyncio.Queue()

    @dagent.tool(supports_context=True)
    async def custom(*, context: dagent.ToolContext, callbacks):
        calls.append("started")
        handles.append(context.ui)
        value = await context.ui.input("Name")
        calls.append("continued")
        try:
            await context.ui.confirm("Again?")
        except dagent.UserInteractionCancelled:
            return "kept:" + value

    provider = provider_for("tool_custom", {})
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(agent_for(custom), input="ask",
                                 interactions=dagent.UserInteractionOptions(queue.put_nowait)))
        first = await receive(queue)
        await runner.respond_to_interaction(answer(first, "Ada"))
        second = await receive(queue)
        assert first.invocation_id == second.invocation_id and first.interaction_id != second.interaction_id
        await runner.respond_to_interaction(answer(second, abandon=True))
        result = await asyncio.wait_for(task, 5)
        assert calls == ["started", "continued"]
        assert "kept:Ada" in provider.requests[-1]["messages"][-1]["content"]
        assert result.status == "completed"
        with pytest.raises(dagent.UserInteractionUnsupported):
            await handles[0].input("too late")


@pytest.mark.asyncio
async def test_same_batch_waits_and_never_replays_tools(tmp_path):
    calls, queue = [], asyncio.Queue()

    @dagent.tool
    def effect():
        calls.append("effect")
        return "done"

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="effect", name="tool_effect"),
                                ToolCall(id="first", name="tool_ui_input", arguments={"title": "first"}),
                                ToolCall(id="second", name="tool_ui_confirm", arguments={"title": "second"}),
                                ToolCall(id="effect2", name="tool_effect")]),
        ChatResponse(content="done"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(agent_for(effect, *dagent.ui_tools()), input="ask",
                                 interactions=dagent.UserInteractionOptions(queue.put_nowait)))
        first = await receive(queue)
        assert calls == ["effect"] and queue.empty() and len(provider.requests) == 1
        with pytest.raises(dagent.RunNotSteerableError):
            await runner.steer(first.run_id, "redirect")
        await runner.respond_to_interaction(answer(first, "one"))
        second = await receive(queue)
        assert calls == ["effect"] and len(provider.requests) == 1
        with pytest.raises(dagent.UserInteractionResponseRejected):
            await runner.respond_to_interaction(answer(first, "old"))
        await runner.respond_to_interaction(answer(second, False))
        result = await asyncio.wait_for(task, 5)
        assert result.status == "completed" and calls == ["effect", "effect"]


@pytest.mark.asyncio
async def test_bad_responses_do_not_consume_or_cross_runs(tmp_path):
    queue = asyncio.Queue()
    with closing(dagent.Runner(provider=provider_for("tool_ui_confirm"), workspace=tmp_path, skill_roots=[])) as a:
        with closing(dagent.Runner(provider=provider_for("tool_ui_select", {"title": "select", "options": [{"id": "a", "label": "A"}]}),
                                   workspace=tmp_path / "b", skill_roots=[])) as b:
            tasks = [asyncio.create_task(runner.run(agent_for(*dagent.ui_tools()), input="ask", run_id="same",
                         interactions=dagent.UserInteractionOptions(queue.put_nowait))) for runner in (a, b)]
            one, two = await receive(queue), await receive(queue)
            requests = {request.type: request for request in (one, two)}
            first, second = requests["confirm"], requests["select"]
            for response in [answer(first, "false"), answer(first, False, invocation_id="other"),
                             answer(first, False, run_id="other"), answer(second, "a")]:
                with pytest.raises(dagent.UserInteractionResponseRejected):
                    await a.respond_to_interaction(response)
            with pytest.raises(ValidationError):
                answer(first, 0)
            with pytest.raises(dagent.UserInteractionResponseRejected):
                await b.respond_to_interaction(answer(second, "outside"))
            await a.respond_to_interaction(answer(first, False))
            with pytest.raises(dagent.UserInteractionResponseRejected):
                await a.respond_to_interaction(answer(first, True))
            assert not tasks[1].done()
            await b.respond_to_interaction(answer(second, "a"))
            assert all(result.status == "completed" for result in await asyncio.gather(*tasks))


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,error", [("timeout", dagent.UserInteractionTimeout),
                                           ("unavailable", dagent.UserInteractionUnavailable)])
@pytest.mark.parametrize("stream", [False, True, "persistent"])
async def test_host_abort_is_fatal_and_auditable(tmp_path, reason, error, stream):
    queue, ended, events = asyncio.Queue(), [], []
    provider = provider_for()
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        options = dagent.UserInteractionOptions(queue.put_nowait, ended.append)

        async def execute():
            if not stream:
                return await runner.run(agent_for(*dagent.ui_tools()), input="ask", interactions=options)
            async for event in runner.stream(agent_for(*dagent.ui_tools()), input="ask", interactions=options,
                    persistence=dagent.StreamPersistenceOptions() if stream == "persistent" else None):
                events.append(event)
                if event.type == "conversation.changed":
                    await runner.acknowledge_changes(event.run_id, event.data.batch_id)

        task = asyncio.create_task(execute())
        request = await receive(queue)
        await runner.abort_interaction(run_id=request.run_id, invocation_id=request.invocation_id,
                                       interaction_id=request.interaction_id, reason=reason)
        if not stream:
            with pytest.raises(error) as raised:
                await asyncio.wait_for(task, 5)
            assert raised.value.result.status == "failed"
            assert raised.value.result.error.code == error.__name__
            assert raised.value.result.checkpoint is None
        else:
            await asyncio.wait_for(task, 5)
            terminal = events[-1]
            if stream == "persistent":
                assert terminal.data.status == "failed" and terminal.data.error.code == error.__name__
            else:
                assert terminal.type == "run.failed" and terminal.data.error_type == error.__name__
                assert terminal.data.result.status == "failed"
        assert len(provider.requests) == 1 and len(ended) == 1 and ended[0].invalidated
        with pytest.raises(dagent.UserInteractionResponseRejected):
            await runner.respond_to_interaction(answer(request, "late"))


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "close", "task_cancel"])
async def test_stop_and_close_cleanup_direct_run(tmp_path, action):
    queue, ended = asyncio.Queue(), []
    runner = dagent.Runner(provider=provider_for(), workspace=tmp_path, skill_roots=[])
    try:
        task = asyncio.create_task(runner.run(agent_for(*dagent.ui_tools()), input="ask",
                                 interactions=dagent.UserInteractionOptions(queue.put_nowait, ended.append)))
        request = await receive(queue)
        if action == "cancel":
            assert await runner.cancel(request.run_id)
        elif action == "close":
            runner.close()
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert len(ended) == 1 and ended[0].reason == ("runner_closed" if action == "close" else "run_cancelled")
        with pytest.raises(dagent.UserInteractionResponseRejected):
            await runner.respond_to_interaction(answer(request, "late"))
        assert not runner._interaction_controls and not runner._active_run_tasks
    finally:
        runner.close()


@pytest.mark.asyncio
async def test_sdk_deadline_is_fatal_even_if_custom_tool_catches_it(tmp_path):
    queue = asyncio.Queue()

    @dagent.tool(supports_context=True)
    async def custom(*, context, callbacks):
        try:
            await context.ui.input("ask")
        except Exception:
            return "swallowed"

    provider = provider_for("tool_custom", {})
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(agent_for(custom), input="ask",
                                 interactions=dagent.UserInteractionOptions(queue.put_nowait, timeout_seconds=3600)))
        request = await receive(queue)
        control = runner._interaction_controls[request.run_id]
        control._expire(control.pending)
        with pytest.raises(dagent.UserInteractionTimeout):
            await asyncio.wait_for(task, 5)
        assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_no_receiver_preserves_tool_failure_flow(tmp_path):
    provider = provider_for()
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = await runner.run(agent_for(*dagent.ui_tools()), input="ask")
        assert result.status == "completed"
        execution = next(node.capability_execution for node in result.trace.root.children if node.kind == "capability_call")
        assert execution.result.stop_reason == "UserInteractionUnsupported"
        assert not runner._interaction_controls


@pytest.mark.asyncio
async def test_notification_failure_is_fatal(tmp_path):
    def broken(request):
        raise OSError("receiver disconnected")

    with closing(dagent.Runner(provider=provider_for(), workspace=tmp_path, skill_roots=[])) as runner:
        with pytest.raises(dagent.UserInteractionDeliveryError) as raised:
            await runner.run(agent_for(*dagent.ui_tools()), input="ask", interactions=dagent.UserInteractionOptions(broken))
        assert raised.value.result.status == "failed" and not runner._interaction_controls


def test_options_and_select_parameters_are_explicit_and_strict():
    for timeout in [0, -1, True, float("inf"), float("nan"), "30"]:
        with pytest.raises(ValueError):
            dagent.UserInteractionOptions(lambda request: None, timeout_seconds=timeout)
    with pytest.raises(TypeError):
        dagent.UserInteractionOptions(None)


@pytest.mark.asyncio
async def test_long_result_waits_for_persistence_ack_and_is_readable(tmp_path):
    from tests.test_conversation_changes import Host

    queue, batches, events = asyncio.Queue(), asyncio.Queue(), []
    read_call = ToolCall(id="read", name="tool_read_file", arguments={})
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call", name="tool_ui_editor", arguments={"title": "Edit", "content": "old", "format": "markdown"})]),
        ChatResponse(tool_calls=[read_call]),
        ChatResponse(content="done"),
    ])
    value = "edited evidence\n" * 500
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[],
                              result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024))) as runner:
        host = Host(runner=runner)

        async def consume():
            async for event in runner.stream(agent_for(*dagent.ui_tools(), "tool.read_file"), input="edit",
                    interactions=dagent.UserInteractionOptions(queue.put_nowait),
                    persistence=dagent.StreamPersistenceOptions()):
                events.append(event)
                if event.type == "conversation.changed":
                    await host.save(event.data)
                    if any(isinstance(record.item, dagent.ToolResultMessage) and record.item.call_id == "call"
                           for record in event.data.messages):
                        batches.put_nowait(event.data)
                    else:
                        await runner.acknowledge_changes(event.run_id, event.data.batch_id)

        task = asyncio.create_task(consume())
        request = await receive(queue)
        await runner.respond_to_interaction(answer(request, value))
        batch = await receive(batches)
        assert len(provider.requests) == 1 and not task.done()
        observation = next(o for o in batch.observations if o.type == "tool_execution")
        assert observation.assistant_message_id == request.assistant_message_id
        assert observation.call_id == request.invocation_id
        item = next(record.item for record in batch.messages if isinstance(record.item, dagent.ToolResultMessage))
        assert observation.result_item_id == item.id
        assert isinstance(item.content, dagent.ContentReference)
        resource = next(resource for resource in batch.resources if resource.reference.path == item.content.path)
        assert runner.resolve_runtime_path(item.content.path, workspace_path=resource.workspace_path,
                                           runtime_directory=resource.runtime_directory).read_text() == value
        assert any(event.type == "user_interaction.ended" for event in events)
        read_call.arguments.update(path=item.content.path)
        await runner.acknowledge_changes(request.run_id, batch.batch_id)
        await asyncio.wait_for(task, 5)
        assert events[-1].data.status == "completed" and len(provider.requests) == 3
        assert item.content.path in provider.requests[1]["messages"][-1]["content"]
        assert "edited evidence" in provider.requests[-1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_reused_provider_call_id_does_not_reuse_interaction_identity(tmp_path):
    queue = asyncio.Queue()
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="same", name="tool_ui_input", arguments={"title": str(index)})])
        for index in range(2)
    ] + [ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(agent_for(*dagent.ui_tools()), input="ask",
                                 interactions=dagent.UserInteractionOptions(queue.put_nowait)))
        first = await receive(queue)
        await runner.respond_to_interaction(answer(first, "first"))
        second = await receive(queue)
        assert first.assistant_message_id != second.assistant_message_id
        assert first.invocation_id == second.invocation_id == "same"
        with pytest.raises(dagent.UserInteractionResponseRejected):
            await runner.respond_to_interaction(answer(first, "old"))
        await runner.respond_to_interaction(answer(second, "second"))
        assert (await asyncio.wait_for(task, 5)).status == "completed"


@pytest.mark.asyncio
async def test_concurrent_runs_on_same_runner_are_independent(tmp_path):
    queue = asyncio.Queue()
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="same", name="tool_ui_input", arguments={"title": "ask"})]),
        ChatResponse(tool_calls=[ToolCall(id="same", name="tool_ui_input", arguments={"title": "ask"})]),
        ChatResponse(content="done"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        tasks = [asyncio.create_task(runner.run(agent_for(*dagent.ui_tools()), input="ask", run_id=f"run_{index}",
                   interactions=dagent.UserInteractionOptions(queue.put_nowait))) for index in range(2)]
        one, two = await receive(queue), await receive(queue)
        with pytest.raises(dagent.UserInteractionResponseRejected):
            await runner.respond_to_interaction(answer(one, "wrong", run_id=two.run_id))
        assert await runner.cancel(one.run_id)
        with pytest.raises(asyncio.CancelledError):
            await tasks[int(one.run_id[-1])]
        await runner.respond_to_interaction(answer(two, "right"))
        assert (await tasks[int(two.run_id[-1])]).status == "completed"


@pytest.mark.asyncio
async def test_pending_request_is_not_replaced_by_concurrent_request(tmp_path):
    queue, busy = asyncio.Queue(), asyncio.Event()

    @dagent.tool(supports_context=True)
    async def custom(*, context, callbacks):
        first = asyncio.create_task(context.ui.input("first"))
        try:
            await busy.wait()
            with pytest.raises(dagent.UserInteractionBusy):
                await context.ui.confirm("second")
            return await first
        finally:
            if not first.done():
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)

    with closing(dagent.Runner(provider=provider_for("tool_custom", {}), workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(agent_for(custom), input="ask",
                                 interactions=dagent.UserInteractionOptions(queue.put_nowait)))
        request = await receive(queue)
        busy.set()
        # Run the second request before the host answers the first.
        await asyncio.sleep(0)
        await runner.respond_to_interaction(answer(request, "first value"))
        assert (await asyncio.wait_for(task, 5)).status == "completed" and queue.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("independent", [False, True])
async def test_nested_agent_is_rejected_without_affecting_root_interaction(tmp_path, independent):
    queue = asyncio.Queue()
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="delegate", name="agent_helper", arguments={"task": "ask"})]),
        *([
            ChatResponse(tool_calls=[ToolCall(id="launch", name="tool_launch")]),
            ChatResponse(tool_calls=[ToolCall(id="independent", name="tool_ui_input", arguments={"title": "independent"})]),
            ChatResponse(content="independent done"),
        ] if independent else []),
        ChatResponse(tool_calls=[ToolCall(id="nested", name="tool_ui_input", arguments={"title": "child"})]),
        ChatResponse(content="child done"),
        ChatResponse(tool_calls=[ToolCall(id="root", name="tool_ui_input", arguments={"title": "root"})]),
        ChatResponse(content="root done"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        @dagent.tool
        async def launch():
            result = await runner.run(agent_for(*dagent.ui_tools()), input="independent", run_id="independent",
                                      interactions=dagent.UserInteractionOptions(queue.put_nowait))
            return result.output_text

        runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[launch, *dagent.ui_tools()]))
        agent = dagent.ToolAgent(profile="conversation", capabilities=list(dagent.ui_tools()), agents=["agent.helper"])
        task = asyncio.create_task(runner.run(agent, input="ask", interactions=dagent.UserInteractionOptions(queue.put_nowait)))
        request = await receive(queue)
        if independent:
            assert request.run_id == "independent" and request.invocation_id == "independent"
            await runner.respond_to_interaction(answer(request, "independent answer"))
            request = await receive(queue)
            assert request.run_id != "independent"
        offset = 3 if independent else 0
        assert request.invocation_id == "root" and len(provider.requests) == 4 + offset
        assert "root ToolLoop" in provider.requests[2 + offset]["messages"][-1]["content"]
        await runner.respond_to_interaction(answer(request, "root answer"))
        assert (await asyncio.wait_for(task, 5)).status == "completed"


@pytest.mark.asyncio
async def test_independent_inner_run_can_be_cancelled_without_cancelling_outer(tmp_path):
    queue, inner_stopped = asyncio.Queue(), []
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="outer_call", name="tool_outer")]),
        ChatResponse(tool_calls=[ToolCall(id="inner_call", name="tool_inner")]),
        ChatResponse(content="done"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        @dagent.tool(supports_context=True)
        async def inner(*, context, callbacks):
            return await context.ui.input("inner")

        @dagent.tool(supports_context=True)
        async def outer(*, context, callbacks):
            try:
                await runner.run(agent_for(inner), input="inner", run_id="inner",
                                 interactions=dagent.UserInteractionOptions(queue.put_nowait))
            except asyncio.CancelledError:
                inner_stopped.append(True)
            return await context.ui.input("outer")

        task = asyncio.create_task(runner.run(agent_for(outer), input="outer", run_id="outer",
                                 interactions=dagent.UserInteractionOptions(queue.put_nowait)))
        first = await receive(queue)
        assert first.run_id == "inner"
        assert await runner.cancel("inner")
        second = await receive(queue)
        assert second.run_id == "outer" and inner_stopped == [True]
        await runner.respond_to_interaction(answer(second, "continued"))
        assert (await asyncio.wait_for(task, 5)).status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["run", "stream", "persistent"])
async def test_review_resume_can_enable_interaction(tmp_path, mode):
    queue, tools = asyncio.Queue(), dagent.ui_tools()
    confirm = tools[0]
    confirm = dagent.CapabilityBinding(confirm.definition.model_copy(update={
        "policy": dagent.CapabilityPolicy(risk="medium")}), confirm.handler, supports_context=True)
    with closing(dagent.Runner(provider=provider_for("tool_ui_confirm"), workspace=tmp_path, skill_roots=[])) as runner:
        initial = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[confirm], review="careful"), input="ask")
        assert initial.requires_review
        checkpoint = dagent.RunCheckpoint.model_validate_json(initial.checkpoint.model_dump_json())
        events = []

        async def resume():
            if mode == "run":
                return await runner.resume(initial.review.approve(), checkpoint=checkpoint,
                           interactions=dagent.UserInteractionOptions(queue.put_nowait))
            async for event in runner.resume_stream(initial.review.approve(), checkpoint=checkpoint,
                    interactions=dagent.UserInteractionOptions(queue.put_nowait),
                    persistence=dagent.StreamPersistenceOptions() if mode == "persistent" else None):
                events.append(event)
                if event.type == "conversation.changed":
                    await runner.acknowledge_changes(event.run_id, event.data.batch_id)
            return None

        task = asyncio.create_task(resume())
        request = await receive(queue)
        assert request.run_id == initial.run_id
        await runner.respond_to_interaction(answer(request, True))
        result = await asyncio.wait_for(task, 5)
        if mode == "run":
            assert result.status == "completed"
        elif mode == "stream":
            assert events[-1].data.result.status == "completed"
        else:
            assert events[-1].data.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["run", "stream", "persistent"])
async def test_sandbox_resume_rejects_interactions_before_consuming_review(tmp_path, monkeypatch, mode):
    queue = asyncio.Queue()
    confirm = dagent.ui_tools()[0]
    confirm = dagent.CapabilityBinding(confirm.definition.model_copy(update={
        "policy": dagent.CapabilityPolicy(risk="medium")}), confirm.handler, supports_context=True)
    provider = provider_for("tool_ui_confirm")
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        initial = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[confirm], review="careful"), input="ask")
        assert initial.requires_review
        checkpoint = initial.checkpoint.model_copy(update={
            "state": initial.checkpoint.state.model_copy(update={"execution": "sandbox"}),
        })

        def unexpected_scope(*args, **kwargs):
            raise AssertionError("Unsupported interactions must not enter the execution scope.")

        monkeypatch.setattr(runner, "_run_scope", unexpected_scope)
        options = dagent.UserInteractionOptions(queue.put_nowait)
        if mode == "run":
            with pytest.raises(dagent.UserInteractionUnsupported):
                await runner.resume(initial.review.approve(), checkpoint=checkpoint, interactions=options)
        else:
            events = []
            async for event in runner.resume_stream(initial.review.approve(), checkpoint=checkpoint, interactions=options,
                    persistence=dagent.StreamPersistenceOptions() if mode == "persistent" else None):
                events.append(event)
                if event.type == "conversation.changed":
                    await runner.acknowledge_changes(event.run_id, event.data.batch_id)
            assert events[-1].type == "run.failed"
            assert events[-1].data.error_type == "UserInteractionUnsupported"
        assert initial.review.review_id not in runner._consumed_review_ids
        assert len(provider.requests) == 1 and queue.empty()


@pytest.mark.asyncio
async def test_unsupported_execution_and_direct_capability(tmp_path):
    queue, tools = asyncio.Queue(), dagent.ui_tools()
    with closing(dagent.Runner(provider=MockProvider([]), workspace=tmp_path, skill_roots=[], capabilities=tools)) as runner:
        options = dagent.UserInteractionOptions(queue.put_nowait)
        for target, kwargs in [(dagent.Dag("test"), {}), (agent_for(*tools), {"execution": "sandbox"})]:
            with pytest.raises(dagent.UserInteractionUnsupported):
                await runner.run(target, interactions=options, **kwargs)
        result = await runner.test_capability("tool.ui_input", {"title": "ask"})
        assert result.stop_reason == "UserInteractionUnsupported" and queue.empty()


@pytest.mark.asyncio
async def test_expired_deadline_cannot_be_won_by_late_response(tmp_path):
    queue = asyncio.Queue()
    with closing(dagent.Runner(provider=provider_for(), workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(agent_for(*dagent.ui_tools()), input="ask",
                 interactions=dagent.UserInteractionOptions(queue.put_nowait, timeout_seconds=3600)))
        request = await receive(queue)
        pending = runner._interaction_controls[request.run_id].pending
        assert pending.timer is not None
        pending.deadline = asyncio.get_running_loop().time() - 1
        with pytest.raises(dagent.UserInteractionResponseRejected):
            await runner.respond_to_interaction(answer(request, "late"))
        with pytest.raises(dagent.UserInteractionTimeout):
            await asyncio.wait_for(task, 5)
        assert pending.timer.cancelled()


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("action", ["close_stream", "close_runner", "cancel"])
async def test_stream_wait_cleanup(tmp_path, persistent, action):
    queue, ended = asyncio.Queue(), []
    with closing(dagent.Runner(provider=provider_for(), workspace=tmp_path, skill_roots=[])) as runner:
        stream = runner.stream(agent_for(*dagent.ui_tools()), input="ask",
                 interactions=dagent.UserInteractionOptions(queue.put_nowait, ended.append),
                 persistence=dagent.StreamPersistenceOptions() if persistent else None)
        async for event in stream:
            if event.type == "conversation.changed":
                await runner.acknowledge_changes(event.run_id, event.data.batch_id)
            if event.type == "user_interaction.requested":
                break
        request = await receive(queue)
        if action == "close_stream":
            await stream.aclose()
        elif action == "close_runner":
            runner.close()
            # A persistent reader finishes normally on Runner close.
            if persistent:
                assert [event async for event in stream] == []
            else:
                with pytest.raises(asyncio.CancelledError):
                    _ = [event async for event in stream]
        else:
            assert await runner.cancel(request.run_id)
            if persistent:
                remaining = []
                async for event in stream:
                    remaining.append(event)
                    if event.type == "conversation.changed":
                        await runner.acknowledge_changes(event.run_id, event.data.batch_id)
                assert remaining[-1].data.status == "cancelled"
            else:
                with pytest.raises(asyncio.CancelledError):
                    _ = [event async for event in stream]
        await stream.aclose()
        assert len(ended) == 1 and ended[0].invalidated
        with pytest.raises(dagent.UserInteractionResponseRejected):
            await runner.respond_to_interaction(answer(request, "late"))
        assert not runner._interaction_controls and not runner._active_run_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_wait", [False, True])
async def test_steering_before_and_after_wait_keeps_existing_rules(tmp_path, cancel_wait):
    from tests.test_steering import GatedProvider

    queue, entered, release = asyncio.Queue(), asyncio.Event(), asyncio.Event()
    waiters, ended = [], []

    @dagent.tool(supports_context=True)
    async def custom(*, context, callbacks):
        entered.set()
        await release.wait()
        waiter = asyncio.create_task(context.ui.input("ask"))
        waiters.append(waiter)
        try:
            return await waiter
        except asyncio.CancelledError:
            return "interaction wait cancelled"

    provider = GatedProvider([
        ChatResponse(tool_calls=[ToolCall(id="call", name="tool_custom")]),
        ChatResponse(content="stale"), ChatResponse(content="redirected"),
    ], blocked={1})
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(agent_for(custom), input="ask", run_id="steer",
                                 interactions=dagent.UserInteractionOptions(queue.put_nowait, ended.append)))
        await asyncio.wait_for(entered.wait(), 5)
        before = await runner.steer("steer", "first update")
        release.set()
        request = await receive(queue)
        with pytest.raises(dagent.RunNotSteerableError):
            await runner.steer("steer", "during wait")
        if cancel_wait:
            waiters[0].cancel()
        else:
            await runner.respond_to_interaction(answer(request, "answer"))
        await asyncio.wait_for(provider.started[1].wait(), 5)
        assert len(ended) == 1 and ended[0].invalidated == cancel_wait
        with pytest.raises(dagent.UserInteractionResponseRejected):
            await runner.respond_to_interaction(answer(request, "late"))
        after = await runner.steer("steer", "second update")
        provider.release[1].set()
        result = await asyncio.wait_for(task, 5)
        assert result.output_text == "redirected"
        ids = {item.id for item in result.conversation.items}
        assert {before.steer_id, after.steer_id} <= ids


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["tool", "dag"])
async def test_auto_agent_scope(tmp_path, route):
    queue = asyncio.Queue()
    provider = MockProvider([ChatResponse(content=route),
        ChatResponse(tool_calls=[ToolCall(id="call", name="tool_ui_confirm", arguments={"title": "Ask"})]),
        ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(dagent.AutoAgent(capabilities=list(dagent.ui_tools())), input="ask",
                                 interactions=dagent.UserInteractionOptions(queue.put_nowait)))
        if route == "dag":
            with pytest.raises(dagent.UserInteractionUnsupported):
                await asyncio.wait_for(task, 5)
            assert len(provider.requests) == 1 and queue.empty()
        else:
            request = await receive(queue)
            await runner.respond_to_interaction(answer(request, True))
            assert (await asyncio.wait_for(task, 5)).status == "completed"


@pytest.mark.asyncio
async def test_interactions_are_not_added_or_inherited_implicitly(tmp_path):
    requests = []
    for options in [None, dagent.UserInteractionOptions(lambda request: pytest.fail("unexpected request"))]:
        provider = MockProvider([ChatResponse(content="done")])
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            assert not any(item.id.startswith("tool.ui_") for item in runner.capabilities)
            await runner.run(agent_for(), input="same input", interactions=options,
                             run_id="wire", workspace_path=tmp_path / "same")
            requests.append(provider.requests)
    assert requests[0] == requests[1]


@pytest.mark.asyncio
async def test_disabled_model_tool_does_not_disable_custom_context(tmp_path):
    queue, tools = asyncio.Queue(), dagent.ui_tools()

    @dagent.tool(supports_context=True)
    async def custom(*, context, callbacks):
        return await context.ui.confirm("custom")

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="disabled", name="tool_ui_confirm", arguments={"title": "direct"}),
                                ToolCall(id="custom", name="tool_custom")]),
        ChatResponse(content="done"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[], capabilities=[*tools, custom])) as runner:
        runner.set_capability_enabled("tool.ui_confirm", False)
        task = asyncio.create_task(runner.run(agent_for("tool.ui_input", custom), input="ask",
                                 interactions=dagent.UserInteractionOptions(queue.put_nowait)))
        request = await receive(queue)
        assert request.invocation_id == "custom" and queue.empty()
        await runner.respond_to_interaction(answer(request, False))
        assert (await asyncio.wait_for(task, 5)).status == "completed"


@pytest.mark.asyncio
async def test_automatic_timeout_and_end_notification_failure(tmp_path):
    requests, ended = [], []
    with closing(dagent.Runner(provider=provider_for(), workspace=tmp_path, skill_roots=[])) as runner:
        with pytest.raises(dagent.UserInteractionTimeout) as raised:
            await asyncio.wait_for(runner.run(agent_for(*dagent.ui_tools()), input="ask",
                interactions=dagent.UserInteractionOptions(requests.append, ended.append, timeout_seconds=.001)), 5)
        assert raised.value.result.status == "failed" and len(requests) == len(ended) == 1
        assert ended[0].reason == "timeout"

    queue = asyncio.Queue()

    def broken_end(event):
        raise OSError("end notification failed")

    with closing(dagent.Runner(provider=provider_for(), workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(agent_for(*dagent.ui_tools()), input="ask",
                interactions=dagent.UserInteractionOptions(queue.put_nowait, broken_end)))
        request = await receive(queue)
        await runner.respond_to_interaction(answer(request, "answer"))
        with pytest.raises(dagent.UserInteractionDeliveryError):
            await asyncio.wait_for(task, 5)
        assert not runner._interaction_controls


@pytest.mark.asyncio
async def test_rejected_result_persistence_does_not_dispatch_or_replay(tmp_path):
    queue, provider = asyncio.Queue(), provider_for()
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        async def consume():
            events = []
            async for event in runner.stream(agent_for(*dagent.ui_tools()), input="ask",
                    interactions=dagent.UserInteractionOptions(queue.put_nowait),
                    persistence=dagent.StreamPersistenceOptions()):
                events.append(event)
                if event.type == "conversation.changed":
                    batch = event.data
                    if any(isinstance(record.item, dagent.ToolResultMessage) for record in batch.messages):
                        await runner.reject_changes(event.run_id, batch.batch_id, message="storage failed")
                    else:
                        await runner.acknowledge_changes(event.run_id, batch.batch_id)
            return events

        task = asyncio.create_task(consume())
        request = await receive(queue)
        await runner.respond_to_interaction(answer(request, "answer"))
        events = await asyncio.wait_for(task, 5)
        assert events[-1].type == "run.failed" and len(provider.requests) == 1
        assert events[-1].data.error_type == "PersistenceRejected"


def test_context_equality_preserves_existing_fields():
    from dagent.harness_runtime.capability_executor import CapabilityExecutionContext

    assert CapabilityExecutionContext(task_id="same") == CapabilityExecutionContext(task_id="same")


def test_select_tool_has_resolvable_provider_schema():
    from jsonschema import Draft202012Validator

    schema = next(binding.definition.parameters for binding in dagent.ui_tools()
                  if binding.definition.id == "tool.ui_select")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate({"title": "Choose", "options": [{"id": "a", "label": "A"}]})
    assert list(validator.iter_errors({"title": "Choose", "options": []}))
