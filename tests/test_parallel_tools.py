"""Opt-in overlapping tool calls preserve ordered transcripts and SDK gates."""

import asyncio
from contextlib import closing
from contextvars import ContextVar
import hashlib
import inspect
import json
from pathlib import Path
import threading

import pytest

import dagent
from dagent.capabilities.workspace import current_workspace_root
from dagent.harness_runtime import result_storage
from dagent.harness_runtime.steering import RunSteeringControl
from dagent.providers import ChatResponse, MockProvider, ToolCall
from tests.test_conversation_changes import Host, consume


def call(call_id, name="tool_work", **arguments):
    return ToolCall(id=str(call_id), name=name, arguments=arguments)


def agent(*tools, concurrency=2, **kwargs):
    return dagent.ToolAgent(profile="conversation", capabilities=tools,
                            max_tool_concurrency=concurrency, **kwargs)


def results(result):
    return [item for item in result.conversation.items if isinstance(item, dagent.ToolResultMessage)]


async def wait(event):
    await asyncio.wait_for(event.wait(), 5)


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "2", None])
@pytest.mark.parametrize("kind", [dagent.ToolAgent, dagent.AutoAgent])
def test_concurrency_requires_positive_integer(value, kind):
    with pytest.raises(ValueError, match="positive integer"):
        kind(profile="conversation", max_tool_concurrency=value)


@pytest.mark.parametrize("kind", [dagent.ToolAgent, dagent.AutoAgent])
def test_new_setting_preserves_released_positional_parameters(kind):
    expected = (["profile", "planner_profile", "name", "max_steps", "capabilities", "skills", "agents", "review",
                 "dynamic_adjust", "context", "response_retries"] if kind is dagent.AutoAgent else
                ["profile", "name", "max_steps", "capabilities", "skills", "agents", "review", "description",
                 "context", "response_retries"])
    signature = inspect.signature(kind)
    positional = [name for name, parameter in signature.parameters.items()
                  if parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD]
    assert positional == expected
    assert kind.__match_args__ == tuple(expected)
    assert signature.parameters["max_tool_concurrency"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
async def test_rolling_pool_overlaps_tools_and_commits_in_model_order(tmp_path, persistent):
    started = [asyncio.Event() for _ in range(4)]
    releases = [asyncio.Event() for _ in range(4)]
    active = peak = 0

    @dagent.tool(parallel_safe=True)
    async def work(index: int):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started[index].set()
        try:
            await releases[index].wait()
            return {"index": index}
        finally:
            active -= 1

    provider = MockProvider([
        ChatResponse(tool_calls=[call(i, index=i) for i in range(4)]), ChatResponse(content="done"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        events = []
        host = Host(runner=runner)

        async def collect():
            if persistent:
                return await consume(runner, runner.stream(agent(work), input="work",
                                     persistence=dagent.StreamPersistenceOptions()), host)
            async for event in runner.stream(agent(work), input="work"):
                events.append(event)
            return events[-1].data.result, events

        task = asyncio.create_task(collect())
        try:
            await wait(started[0])
            await wait(started[1])
            assert not started[2].is_set()
            releases[1].set()
            await wait(started[2])  # A free slot is reused while call 0 is blocked.
            releases[2].set()
            await wait(started[3])
            assert active <= 2
            releases[3].set()
            releases[0].set()
            result, observed = await asyncio.wait_for(task, 5)
        finally:
            for release in releases:
                release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert result.status == "completed"
        assert peak == 2
        assert [item.call_id for item in results(result)] == ["0", "1", "2", "3"]
        assert [item.value for item in results(result)] == [{"index": i} for i in range(4)]
        assert result.usage.capability_calls == 4
        assert result.usage.model_turns == len(provider.requests) == 2
        if persistent:
            completed = [o.call_id for b in host.batches for o in b.observations
                         if o.type == "tool_execution" and o.status == "completed"]
            persisted = [r.item.call_id for b in host.batches for r in b.messages
                         if isinstance(r.item, dagent.ToolResultMessage)]
            assert persisted == ["0", "1", "2", "3"]
            assert completed.index("1") < completed.index("0")
            assert host.state == result.conversation
            assert result.trace is None
        else:
            completed = [e.data.invocation_id for e in observed if e.type == "capability.call.completed"]
            assert completed.index("1") < completed.index("0")


@pytest.mark.asyncio
async def test_exclusive_tools_are_barriers_between_parallel_groups(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    order = []

    @dagent.tool(parallel_safe=True)
    async def work(index: int):
        order.append(f"start{index}")
        if index == 0:
            started.set()
            await release.wait()
        order.append(f"end{index}")
        return str(index)

    @dagent.tool
    def mutate():
        assert order.count("end0") == order.count("end1") == 1
        order.append("mutate")
        return "written"

    provider = MockProvider([ChatResponse(tool_calls=[call(0, index=0), call(1, index=1),
                            call("write", "tool_mutate"), call(2, index=2)]), ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(agent(work, mutate), input="work"))
        await wait(started)
        release.set()
        result = await asyncio.wait_for(task, 5)
    assert result.status == "completed"
    assert order.index("mutate") < order.index("start2")
    assert order.index("end1") < order.index("mutate")


@pytest.mark.asyncio
async def test_synchronous_decorated_tools_overlap_in_threads_with_context(tmp_path):
    barrier = threading.Barrier(2)
    marker = ContextVar("parallel_test_marker", default="missing")
    marker.set("caller")
    caller_thread = threading.get_ident()

    @dagent.tool(parallel_safe=True, supports_context=True)
    def work(index: int, *, context, callbacks):
        assert threading.get_ident() != caller_thread
        assert marker.get() == "caller"
        assert current_workspace_root(tmp_path) == context.workspace_path.resolve()
        barrier.wait(timeout=5)
        return {"index": index, "task": context.task_id}

    provider = MockProvider([ChatResponse(tool_calls=[call(i, index=i) for i in range(2)]),
                             ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = await asyncio.wait_for(runner.run(agent(work), input="work", run_id="threaded"), 10)
    assert [item.status for item in results(result)] == ["completed", "completed"], [item.content.text for item in results(result)]
    assert [item.value for item in results(result)] == [dict(index=i, task="threaded") for i in range(2)]


@pytest.mark.asyncio
async def test_threaded_tool_callbacks_run_on_the_execution_loop(tmp_path):
    thread = threading.get_ident()
    tokens, events = [], []

    @dagent.tool(parallel_safe=True, supports_context=True)
    def work(*, context, callbacks):
        assert threading.get_ident() != thread
        callbacks.on_token("progress")
        callbacks.on_event({"type": "tool_progress"})
        return "done"

    provider = MockProvider([ChatResponse(tool_calls=[call(0)]), ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = await runner.run(agent(work), input="work",
                                  on_token=lambda value: tokens.append((value, threading.get_ident())),
                                  on_event=lambda value: events.append((value, threading.get_ident())))
    assert result.status == "completed"
    assert ("progress", thread) in tokens
    assert [(event, owner) for event, owner in events if event["type"] == "tool_progress"] == [
        ({"type": "tool_progress"}, thread),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("debug", [False, True])
async def test_threaded_progress_reaches_stream_before_tool_returns(tmp_path, persistent, debug):
    loop = asyncio.get_running_loop()
    previous_debug = loop.get_debug()
    entered = asyncio.Event()
    emit_allowed, host_handled = threading.Event(), threading.Event()
    observed = []

    @dagent.tool(parallel_safe=True, supports_context=True)
    def work(*, context, callbacks):
        loop.call_soon_threadsafe(entered.set)
        assert emit_allowed.wait(timeout=5)
        callbacks.on_event({"type": "response_token", "channel": "content", "delta": "tool progress"})
        assert host_handled.wait(timeout=5), "The host must receive progress before the tool returns"
        return "host handled progress"

    provider = MockProvider([ChatResponse(tool_calls=[call(0)]), ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)

        async def collect():
            options = {"persistence": dagent.StreamPersistenceOptions()} if persistent else {}
            async for event in runner.stream(agent(work), input="work", **options):
                observed.append(event)
                if event.type == "response.content.delta" and event.data.delta == "tool progress":
                    host_handled.set()
                if event.type == "conversation.changed":
                    await host.save(event.data)
                    await runner.acknowledge_changes(event.run_id, event.data.batch_id)

        loop.set_debug(debug)
        task = asyncio.create_task(collect())
        try:
            await wait(entered)
            await asyncio.sleep(0)  # Let the stream reader wait for the worker's event.
            emit_allowed.set()
            await asyncio.wait_for(task, 10)
        finally:
            emit_allowed.set()
            host_handled.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            loop.set_debug(previous_debug)
    conversation = host.state if persistent else observed[-1].data.result.conversation
    tool_result = next(item for item in conversation.items if isinstance(item, dagent.ToolResultMessage))
    assert tool_result.status == "completed", tool_result.content
    assert tool_result.content.text == "host handled progress"
    assert any(event.type == "response.content.delta" and event.data.delta == "tool progress" for event in observed)
    if persistent:
        assert host.batches[-1].status == "completed"
    else:
        assert observed[-1].data.result.status == "completed"


@pytest.mark.asyncio
async def test_default_serial_keeps_synchronous_tool_on_callers_thread(tmp_path):
    thread = threading.get_ident()
    seen = []

    @dagent.tool(parallel_safe=True)
    def work(index: int):
        assert threading.get_ident() == thread
        seen.append(index)
        return index

    provider = MockProvider([ChatResponse(tool_calls=[call(i, index=i) for i in range(2)]),
                             ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = await runner.run(agent(work, concurrency=1), input="work")
    assert seen == [0, 1]
    assert "max_tool_concurrency" not in result.plan.model_dump(mode="json")
    assert "parallel_safe_capability_ids" not in result.plan.model_dump(mode="json")


@pytest.mark.asyncio
async def test_review_freezes_parallel_policy_and_restores_on_new_runner(tmp_path):
    seen = []
    both = asyncio.Event()

    @dagent.tool(parallel_safe=True, risk="medium")
    async def work(index: int):
        seen.append(index)
        if len(seen) == 2:
            both.set()
        await both.wait()
        return str(index)

    provider = MockProvider([ChatResponse(tool_calls=[call(i, index=i) for i in range(2)]),
                             ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as first:
        paused = await first.run(agent(work, review="careful"), input="work")
        checkpoint = dagent.RunCheckpoint.model_validate_json(paused.checkpoint.model_dump_json())
        assert seen == []
        assert checkpoint.plan.max_tool_concurrency == 2
        assert checkpoint.plan.parallel_safe_capability_ids == ("tool.work",)
    with closing(dagent.Runner(provider=provider, capabilities=[work], workspace=tmp_path, skill_roots=[])) as second:
        result = await asyncio.wait_for(second.resume(checkpoint.review.approve(), checkpoint=checkpoint), 5)
    assert result.status == "completed" and sorted(seen) == [0, 1]
    assert [item.call_id for item in results(result)] == ["0", "1"]


@pytest.mark.asyncio
async def test_resume_rejects_changed_parallel_declaration(tmp_path):
    @dagent.tool(parallel_safe=True, risk="medium")
    def work():
        return "ok"

    provider = MockProvider([ChatResponse(tool_calls=[call(0)])])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as first:
        paused = await first.run(agent(work, review="careful"), input="work")
    changed = dagent.CapabilityBinding(work.definition.model_copy(update={"parallel_safe": False}), work.handler)
    with closing(dagent.Runner(provider=provider, capabilities=[changed], workspace=tmp_path, skill_roots=[])) as second:
        with pytest.raises(ValueError, match="parallel-safe.*changed"):
            await second.resume(paused.review.approve(), checkpoint=paused.checkpoint)


@pytest.mark.asyncio
async def test_steer_drains_inflight_and_skips_only_unstarted_calls(tmp_path):
    started = [asyncio.Event() for _ in range(3)]
    release = asyncio.Event()

    @dagent.tool(parallel_safe=True)
    async def work(index: int):
        started[index].set()
        await release.wait()
        return str(index)

    provider = MockProvider([ChatResponse(tool_calls=[call(i, index=i) for i in range(3)]),
                             ChatResponse(content="redirected")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(agent(work), input="work", run_id="steered"))
        await wait(started[1])
        await runner.steer("steered", "new direction")
        release.set()
        result = await asyncio.wait_for(task, 5)
    assert not started[2].is_set()
    assert [item.status for item in results(result)] == ["completed", "completed", "skipped"]
    assert provider.requests[-1]["messages"][-1]["content"] == "new direction"


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("max_steps", [1, 3])
@pytest.mark.parametrize("first_completed", [0, 1])
async def test_buffered_steer_is_resolved_after_final_parallel_completion(
    tmp_path, monkeypatch, persistent, max_steps, first_completed,
):
    started = [asyncio.Event(), asyncio.Event()]
    releases = [asyncio.Event(), asyncio.Event()]
    drained = asyncio.Event()
    original_drain = RunSteeringControl.drain

    def observe_drain(control, run_id):
        pending = original_drain(control, run_id)
        if pending:
            drained.set()
        return pending

    monkeypatch.setattr(RunSteeringControl, "drain", observe_drain)

    @dagent.tool(parallel_safe=True)
    async def work(index: int):
        started[index].set()
        await releases[index].wait()
        return index

    provider = MockProvider([ChatResponse(tool_calls=[call(i, index=i) for i in range(2)]),
                             ChatResponse(content="redirected")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)

        async def collect():
            if persistent:
                return await consume(runner, runner.stream(agent(work, max_steps=max_steps), input="work",
                                     run_id="buffered-steer", persistence=dagent.StreamPersistenceOptions()), host)
            observed = [event async for event in runner.stream(agent(work, max_steps=max_steps), input="work",
                                                               run_id="buffered-steer")]
            return observed[-1].data.result, observed

        task = asyncio.create_task(collect())
        try:
            await wait(started[0])
            await wait(started[1])
            receipt = await runner.steer("buffered-steer", "new direction")
            releases[first_completed].set()
            await wait(drained)
            releases[1 - first_completed].set()
            result, observed = await asyncio.wait_for(task, 5)
        finally:
            for release in releases:
                release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert [item.call_id for item in results(result)] == ["0", "1"]
    assert [item.status for item in results(result)] == ["completed", "completed"]
    resolutions = [event for event in observed if event.type in {"steer.applied", "steer.discarded"}]
    assert len(resolutions) == 1
    assert resolutions[0].data.steer_id == receipt.steer_id
    if max_steps == 1:
        assert result.status == "failed"
        assert resolutions[0].type == "steer.discarded"
        assert resolutions[0].data.reason == "step_limit_exhausted"
        assert len(provider.requests) == 1
        assert not any(item.id == receipt.steer_id for item in result.conversation.items)
    else:
        assert result.status == "completed"
        assert resolutions[0].type == "steer.applied"
        assert result.conversation.items[-2].id == receipt.steer_id
        assert provider.requests[-1]["messages"][-1]["content"] == "new direction"
    if persistent:
        assert host.state == result.conversation


@pytest.mark.asyncio
async def test_ordinary_failure_does_not_cancel_parallel_sibling(tmp_path):
    started = asyncio.Event()

    @dagent.tool(parallel_safe=True)
    async def work(index: int):
        if index == 0:
            await started.wait()
            raise ValueError("ordinary error")
        started.set()
        return "ok"

    provider = MockProvider([ChatResponse(tool_calls=[call(i, index=i) for i in range(2)]),
                             ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = await asyncio.wait_for(runner.run(agent(work), input="work"), 5)
    assert [item.status for item in results(result)] == ["failed", "completed"]


@pytest.mark.asyncio
async def test_parallel_safe_tools_cannot_open_user_interactions(tmp_path):
    @dagent.tool(parallel_safe=True, supports_context=True)
    async def work(*, context, callbacks):
        return await context.ui.input("not allowed")

    requested = []
    provider = MockProvider([ChatResponse(tool_calls=[call(0)]), ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = await runner.run(agent(work), input="work",
                                  interactions=dagent.UserInteractionOptions(requested.append))
    assert requested == []
    assert results(result)[0].status == "failed"
    assert "Parallel-safe tools cannot request user interaction" in results(result)[0].content.text


@pytest.mark.asyncio
async def test_default_plan_and_builtin_fingerprints_keep_legacy_fields(tmp_path):
    provider = MockProvider([ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = await runner.run(agent("tool.read_file", concurrency=1), input="work")
        definition = runner.get_capability("tool.read_file")
    payload = definition.model_dump(mode="json")
    assert payload.pop("parallel_safe") is True
    expected = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")).encode()).hexdigest()
    assert result.plan.capability_fingerprints["tool.read_file"] == expected
    saved = result.checkpoint.model_dump(mode="json")
    assert "max_tool_concurrency" not in saved["plan"]
    assert "parallel_safe_capability_ids" not in saved["plan"]
    assert dagent.RunCheckpoint.model_validate(saved).plan.fingerprint == result.plan.fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["auto", "delegate", "static"])
async def test_parallel_policy_reaches_auto_and_registered_agents(tmp_path, kind):
    entered = []
    both = asyncio.Event()

    @dagent.tool(parallel_safe=True)
    async def work(index: int):
        entered.append(index)
        if len(entered) == 2:
            both.set()
        await both.wait()
        return str(index)

    responses = [ChatResponse(tool_calls=[call(i, index=i) for i in range(2)]), ChatResponse(content="done")]
    if kind == "auto":
        responses.insert(0, ChatResponse(content="tool"))
        target = dagent.AutoAgent(capabilities=[work], max_tool_concurrency=2)
    else:
        helper = dagent.ToolAgent(profile="conversation", name="helper", capabilities=[work], max_tool_concurrency=2)
        if kind == "delegate":
            responses.insert(0, ChatResponse(tool_calls=[call("child", "agent_helper", task="work")]))
            responses.append(ChatResponse(content="outer done"))
            target = dagent.ToolAgent(profile="conversation", capabilities=[], agents=[helper])
        else:
            target = dagent.Dag("parallel")
            target.add_node(dagent.Node("assistant", target=helper, inputs={"prompt": "work"}))
    with closing(dagent.Runner(provider=MockProvider(responses), workspace=tmp_path, skill_roots=[])) as runner:
        result = await asyncio.wait_for(runner.run(target, **({} if kind == "static" else {"input": "work"})), 5)
    assert result.status == "completed" and sorted(entered) == [0, 1]
    assert "tool.work" in result.plan.parallel_safe_capability_ids


@pytest.mark.asyncio
async def test_storage_failure_cancels_siblings_and_stops_replenishment(tmp_path, monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    seen = []

    @dagent.tool(parallel_safe=True)
    async def work(index: int):
        seen.append(index)
        if index == 0:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        await entered.wait()
        return {"data": "x" * 5000}

    original = result_storage._write_reference

    def fail(*args, **kwargs):
        if kwargs.get("media_type") != "text/plain; charset=utf-8":
            raise OSError("disk full")
        return original(*args, **kwargs)

    monkeypatch.setattr(result_storage, "_write_reference", fail)
    provider = MockProvider([ChatResponse(tool_calls=[call(i, index=i) for i in range(3)])])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[],
                              result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024))) as runner:
        with pytest.raises(dagent.RunExecutionError) as raised:
            await asyncio.wait_for(runner.run(agent(work), input="work"), 5)
    assert cancelled.is_set() and seen == [0, 1]
    result = raised.value.result
    assert result.status == "failed" and result.checkpoint is None
    nodes = {n.capability_execution.invocation.invocation_id: n for n in result.trace.root.children
             if n.capability_execution is not None}
    assert nodes["0"].error.code == "outcome_unknown"
    assert nodes["1"].error.code == "ResultStorageError"
    assert result.usage.capability_calls == 2 and len(provider.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "reject"])
async def test_persistent_interrupt_cleans_up_parallel_tasks(tmp_path, action):
    started = [asyncio.Event() for _ in range(3)]
    cancelled = [asyncio.Event() for _ in range(2)]

    @dagent.tool(parallel_safe=True)
    async def work(index: int):
        started[index].set()
        if action == "reject" and index == 1:
            await started[0].wait()
            return "completed"
        try:
            await asyncio.Event().wait()
        finally:
            cancelled[index].set()

    provider = MockProvider([ChatResponse(tool_calls=[call(i, index=i) for i in range(3)])])
    events = []
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        host = Host(runner=runner)

        async def collect():
            async for event in runner.stream(agent(work), input="work", run_id="interrupt",
                    persistence=dagent.StreamPersistenceOptions()):
                events.append(event)
                if event.type == "conversation.changed":
                    await host.save(event.data)
                    if action == "reject" and any(o.type == "tool_execution" and o.status == "completed"
                                                  for o in event.data.observations):
                        await runner.reject_changes(event.run_id, event.data.batch_id, message="cannot save")
                    else:
                        await runner.acknowledge_changes(event.run_id, event.data.batch_id)

        task = asyncio.create_task(collect())
        if action == "cancel":
            await wait(started[1])
            assert await runner.cancel("interrupt")
        await asyncio.wait_for(task, 5)
    assert cancelled[0].is_set()
    assert not started[2].is_set()
    assert len(provider.requests) == 1
    if action == "cancel":
        assert cancelled[1].is_set()
        observations = [o for b in host.batches for o in b.observations if o.type == "tool_execution"]
        assert {o.call_id for o in observations if o.status == "outcome_unknown"} == {"0", "1"}
        assert host.batches[-1].status == "cancelled"
    else:
        assert events[-1].type == "run.failed"


@pytest.mark.asyncio
async def test_released_0112_checkpoint_resumes_with_new_builtin_declarations(tmp_path):
    # Captured from main at fa695ac before introducing parallel metadata.
    fixture = Path(__file__).parent / "fixtures/tool_parallel_legacy_checkpoint.json"
    checkpoint = dagent.RunCheckpoint.model_validate_json(fixture.read_text())
    checkpoint.state.workspace_path = str(tmp_path)
    (tmp_path / "input.txt").write_text("legacy input")
    provider = MockProvider([ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = await runner.resume(checkpoint.review.approve(), checkpoint=checkpoint)
    assert result.status == "completed"
    assert [item.call_id for item in results(result)] == ["read", "write"]
    assert (tmp_path / "output.txt").read_text() == "hello"
    assert result.plan.max_tool_concurrency == 1


@pytest.mark.asyncio
async def test_builtin_ui_is_an_exclusive_barrier(tmp_path):
    from tests.test_user_interactions import answer

    seen = []
    both = asyncio.Event()
    requests = asyncio.Queue()

    @dagent.tool(parallel_safe=True)
    async def work(index: int):
        seen.append(index)
        if index == 2:
            return "after input"
        if len(seen) == 2:
            both.set()
        await both.wait()
        return "before input"

    provider = MockProvider([ChatResponse(tool_calls=[call(0, index=0), call(1, index=1),
                call("ui", "tool_ui_input", title="Question"), call(2, index=2)]), ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(agent(work, *dagent.ui_tools()), input="work",
                        interactions=dagent.UserInteractionOptions(requests.put_nowait)))
        request = await asyncio.wait_for(requests.get(), 5)
        assert seen == [0, 1]
        await runner.respond_to_interaction(answer(request, "continue"))
        result = await asyncio.wait_for(task, 5)
    assert result.status == "completed" and seen == [0, 1, 2]


@pytest.mark.asyncio
async def test_persistence_ack_precedes_each_parallel_tool_body(tmp_path):
    started = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()
    gate = asyncio.Queue()
    proceed = asyncio.Event()

    @dagent.tool(parallel_safe=True)
    async def work(index: int):
        started[index].set()
        await release.wait()
        return "ok"

    provider = MockProvider([ChatResponse(tool_calls=[call(i, index=i) for i in range(2)]), ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        async def collect():
            async for event in runner.stream(agent(work), input="work", persistence=dagent.StreamPersistenceOptions()):
                if event.type == "conversation.changed":
                    for observation in event.data.observations:
                        if observation.type == "tool_execution" and observation.status == "starting":
                            assert not started[int(observation.call_id)].is_set()
                            gate.put_nowait(observation.call_id)
                            await proceed.wait()
                    await runner.acknowledge_changes(event.run_id, event.data.batch_id)

        task = asyncio.create_task(collect())
        assert await asyncio.wait_for(gate.get(), 5) == "0"
        assert not any(event.is_set() for event in started)
        proceed.set()
        await wait(started[0])
        await wait(started[1])
        release.set()
        await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
async def test_denied_call_does_not_block_parallel_approved_siblings(tmp_path):
    seen = []
    both = asyncio.Event()

    @dagent.tool(parallel_safe=True, risk="medium")
    async def work(index: int):
        seen.append(index)
        if len(seen) == 2:
            both.set()
        await both.wait()
        return str(index)

    provider = MockProvider([ChatResponse(tool_calls=[call(i, index=i) for i in range(3)]), ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        initial = await runner.run(agent(work, review="careful"), input="work")
        decision = initial.review.decide([
            dagent.CapabilityReviewDecision(invocation_id=str(i), approved=i != 1) for i in range(3)
        ])
        result = await asyncio.wait_for(runner.resume(decision, checkpoint=initial.checkpoint), 5)
    assert seen == [0, 2]
    assert [item.status for item in results(result)] == ["completed", "denied", "completed"]


@pytest.mark.asyncio
async def test_cancelled_sync_tool_drops_late_callback_tokens(tmp_path):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    tokens = []

    @dagent.tool(parallel_safe=True, supports_context=True)
    def work(*, context, callbacks):
        entered.set()
        try:
            release.wait(timeout=5)
            callbacks.on_token("late-token")
            return "finished"
        finally:
            finished.set()

    provider = MockProvider([ChatResponse(tool_calls=[call(0)])])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        task = asyncio.create_task(runner.run(agent(work), input="work", run_id="sync-cancel", on_token=tokens.append))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)
    assert "late-token" not in tokens


@pytest.mark.asyncio
async def test_cancel_preserves_completed_out_of_order_results_and_files(tmp_path):
    entered = asyncio.Event()

    @dagent.tool(parallel_safe=True)
    async def work(index: int):
        if index == 0:
            entered.set()
            await asyncio.Event().wait()
        await entered.wait()
        return {"data": "x" * 5000}

    provider = MockProvider([ChatResponse(tool_calls=[call(i, index=i) for i in range(3)])])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[],
                              result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024))) as runner:
        host = Host(runner=runner)
        async for event in runner.stream(agent(work), input="work", run_id="buffered-cancel",
                persistence=dagent.StreamPersistenceOptions()):
            if event.type == "conversation.changed":
                await host.save(event.data)
                await runner.acknowledge_changes(event.run_id, event.data.batch_id)
                if any(o.type == "tool_execution" and o.call_id == "1" and o.status == "completed"
                       for o in event.data.observations):
                    assert await runner.cancel(event.run_id)
    known = [r for r in host.state.items if isinstance(r, dagent.ToolResultMessage)]
    assert [r.call_id for r in known] == ["1"]
    assert known[0].status == "completed" and known[0].value_reference is not None
    assert host.resources
    assert host.batches[-1].status == "cancelled"


@pytest.mark.asyncio
async def test_new_boundary_review_saves_drained_prefix_without_replay(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    path = workspace / "link.txt"
    path.write_text("inside")
    seen = []
    both = asyncio.Event()

    @dagent.tool(parallel_safe=True)
    async def work(index: int):
        seen.append(index)
        if len(seen) == 2:
            both.set()
        await both.wait()
        return str(index)

    @dagent.tool(risk="medium")
    def redirect():
        seen.append("redirect")
        path.unlink()
        path.symlink_to(outside)
        return "redirected"

    provider = MockProvider([ChatResponse(tool_calls=[call(0, index=0), call(1, index=1),
                call("redirect", "tool_redirect"), call("read", "tool_read_file", path="link.txt")]),
                             ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=workspace, skill_roots=[])) as runner:
        first = await runner.run(agent(work, redirect, "tool.read_file", review="careful"),
                                 input="work", workspace_path=workspace)
        assert seen == []
        second = await asyncio.wait_for(runner.resume(first.review.approve(), checkpoint=first.checkpoint), 5)
        assert second.requires_review and seen == [0, 1, "redirect"]
        checkpoint = dagent.RunCheckpoint.model_validate_json(second.checkpoint.model_dump_json())
        assert checkpoint.state.pending_tool_batch.next_index == 3
        assert [item.call_id for item in results(second)] == ["0", "1", "redirect"]
        final = await runner.resume(second.review.approve(), checkpoint=checkpoint)
    assert final.status == "completed" and seen == [0, 1, "redirect"]
    assert results(final)[-1].content.text == "outside"
