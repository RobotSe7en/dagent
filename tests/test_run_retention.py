"""Root completion ordering, portable review and owned child-session eviction."""

import asyncio
from contextlib import closing, contextmanager
import gc
import json
import tracemalloc
import weakref

import pytest

import dagent
from dagent.capabilities.providers import AgentCapabilityProvider, AgentNodeSessionStore
from dagent.capabilities.tools.registry import ToolOutput
from dagent.harness_runtime import result_storage
from dagent.harness_runtime.compaction import CompactionArchiveError, HistoryCompactor
from dagent.providers import ChatResponse, MockProvider, ToolCall
from tests.test_steering import GatedProvider
from tests.test_reasoning_mapping import _endpoint
from tests.test_cache_usage import _completion, _usage
from tests.test_compaction_strategy import checkpoint, request_aliases


@pytest.mark.parametrize("value", [-1, True, 1.5, "2"])
def test_retention_configuration_is_strict(tmp_path, value):
    with pytest.raises(ValueError, match="max_retained_runs"):
        dagent.Runner(provider=MockProvider(), workspace=tmp_path, max_retained_runs=value)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [None, 0, 2])
async def test_finished_root_states_and_checkpoints_have_one_retention_order(tmp_path, limit):
    with closing(dagent.Runner(provider=MockProvider([ChatResponse(content="done")] * 5),
                              workspace=tmp_path, skill_roots=[], max_retained_runs=limit)) as runner:
        agent = dagent.ToolAgent(profile="conversation", capabilities=[])
        saved = []
        for index in range(5):
            result = await runner.run(agent, input="hello", run_id=f"run_{index}")
            saved.append(result)
            assert result.checkpoint is not None
            expected = range(0 if limit is None else max(0, index + 1 - limit), index + 1)
            assert set(runner.runtime.session.runs) == {f"run_{i}" for i in expected}
            assert set(runner._run_checkpoints) == set(runner.runtime.session.runs)
        for index, result in enumerate(saved):
            retained = limit is None or index >= 5 - limit
            assert (runner.run_state(result.run_id) is not None) == retained
            assert (runner.run_trace(result.run_id) is not None) == retained
            assert (runner.run_checkpoint(result.run_id) is not None) == retained
            assert result.status == "completed" and result.checkpoint.state.status == "completed"
            with pytest.raises(ValueError, match="already exists"):
                await runner.run(agent, input="reuse", run_id=result.run_id)
        assert len({result.plan.fingerprint for result in saved}) == 1


def test_derive_inherits_limit_without_history_and_none_clears_it(tmp_path):
    with closing(dagent.Runner(provider=MockProvider(), workspace=tmp_path, skill_roots=[], max_retained_runs=2)) as runner:
        runner._used_run_ids.add("old")
        for options, expected in [({}, 2), ({"max_retained_runs": None}, None), ({"max_retained_runs": 0}, 0)]:
            with closing(runner.derive(**options)) as child:
                assert child.max_retained_runs == expected
                assert not child._used_run_ids and not child.runtime.runs and not child._run_checkpoints
        assert runner.max_retained_runs == 2


def test_from_config_accepts_explicit_process_retention(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text('provider:\n  base_url: "http://local/v1"\n  model: "test"\n')
    with closing(dagent.Runner.from_config(config, workspace=tmp_path, skill_roots=[], max_retained_runs=0)) as runner:
        assert runner.max_retained_runs == 0


@pytest.mark.asyncio
async def test_waiting_review_survives_other_runs_and_finishes_most_recently(tmp_path):
    effects = []

    @dagent.tool(risk="medium")
    def record(index: int) -> str:
        effects.append(index)
        return "ok"

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="first", name="tool_record", arguments={"index": 1})]),
        *[ChatResponse(content="unrelated")] * 12,
        ChatResponse(tool_calls=[ToolCall(id="second", name="tool_record", arguments={"index": 2})]),
        ChatResponse(content="done"),
        *[ChatResponse(content="done")] * 2,
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[], max_retained_runs=2)) as runner:
        agent = dagent.ToolAgent(profile="conversation", capabilities=[record], review="careful")
        first = await runner.run(agent, input="record twice", run_id="old")
        checkpoint1 = first.checkpoint
        for index in range(12):
            await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[]), input="other", run_id=f"other_{index}")
        assert set(runner.runtime.runs) == {"old", "other_10", "other_11"}
        second = await runner.resume(first.review.approve(), checkpoint=checkpoint1)
        assert second.requires_review and effects == [1]
        result = await runner.resume(second.review.approve(), checkpoint=second.checkpoint)
        assert result.status == "completed" and effects == [1, 2]
        assert set(runner.runtime.runs) == {"old", "other_11"}
        assert not runner.runtime.session._review_index
        with pytest.raises(ValueError, match="already been consumed"):
            await runner.resume(first.review.approve(), checkpoint=checkpoint1)
        # The consumed set also survives eviction of the heavy state.
        for index in range(2):
            await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[]), input="other")
        assert runner.run_state("old") is None
        with pytest.raises(ValueError, match="already been consumed"):
            await runner.resume(second.review.approve(), checkpoint=second.checkpoint)


@pytest.mark.asyncio
async def test_exported_pending_checkpoint_needs_no_local_history(tmp_path):
    @dagent.tool(risk="medium")
    def record() -> str:
        return "ok"

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="write", name="tool_record")]), ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[], max_retained_runs=0)) as runner:
        first = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[record], review="careful"), input="write")
        saved = dagent.RunCheckpoint.model_validate_json(first.checkpoint.model_dump_json())
    with closing(dagent.Runner(provider=provider, capabilities=[record], workspace=tmp_path, skill_roots=[], max_retained_runs=0)) as runner:
        result = await runner.resume(first.review.approve(), checkpoint=saved)
        assert result.status == "completed" and result.checkpoint is not None
        assert runner.run_checkpoint(result.run_id) is None
        assert not runner.runtime.session._review_index


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["finalize", "resume_scope"])
async def test_failure_after_review_state_save_does_not_leave_an_immortal_run(tmp_path, monkeypatch, stage):
    @dagent.tool(risk="medium")
    def record() -> str:
        return "ok"

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="write", name="tool_record")])])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[], max_retained_runs=0)) as runner:
        agent = dagent.ToolAgent(profile="conversation", capabilities=[record], review="careful")
        if stage == "finalize":
            def fail(*args, **kwargs):
                raise RuntimeError("finalization failed")
            monkeypatch.setattr(runner, "_finalize_run_result", fail)
            operation = runner.run(agent, input="write", run_id="pending")
        else:
            first = await runner.run(agent, input="write", run_id="pending")
            @contextmanager
            def fail_scope(*args, **kwargs):
                raise RuntimeError("scope failed")
                yield
            monkeypatch.setattr(runner, "_run_scope", fail_scope)
            operation = runner.resume(first.review.approve(), checkpoint=first.checkpoint)
        with pytest.raises(RuntimeError, match="failed"):
            await operation
        assert not runner.runtime.runs and not runner._run_checkpoints
        assert not runner.runtime.session._review_index


@pytest.mark.asyncio
async def test_concurrent_runs_count_by_end_order_and_keep_active_snapshots(tmp_path):
    provider = GatedProvider([ChatResponse(content="first"), ChatResponse(content="second"), ChatResponse(content="third")], blocked={0})
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[], max_retained_runs=1)) as runner:
        agent = dagent.ToolAgent(profile="conversation", capabilities=[])
        slow = asyncio.create_task(runner.run(agent, input="slow", run_id="slow"))
        await asyncio.wait_for(provider.started[0].wait(), 5)
        # A host may save an intermediate terminal-looking snapshot while the
        # root is still running. Retention must never run inside save_run_state.
        runner.runtime.session.save_run_state(dagent.RunState(run_id="slow", kind="tool", status="completed"))
        await runner.run(agent, input="fast", run_id="fast")
        await runner.run(agent, input="last", run_id="last")
        assert set(runner.runtime.runs) == {"slow", "last"}
        provider.release[0].set()
        await slow
        assert set(runner.runtime.runs) == {"slow"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_cancellation_releases_owned_task_cache_and_prevents_id_reuse(tmp_path, stream):
    provider = GatedProvider([ChatResponse(content="never")], blocked={0})
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[], max_retained_runs=0)) as runner:
        store = AgentNodeSessionStore()
        store.save(task_id="cancelled", node_id="node", fingerprint="f", conversation=dagent.ConversationState())
        runner._agent_session_stores["owned"] = store

        async def invoke():
            if stream:
                async for _ in runner.stream(dagent.ToolAgent(profile="conversation"), input="wait", run_id="cancelled"):
                    pass
            else:
                await runner.run(dagent.ToolAgent(profile="conversation"), input="wait", run_id="cancelled")

        task = asyncio.create_task(invoke())
        await asyncio.wait_for(provider.started[0].wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not store._conversations and not runner.runtime.runs and not runner._run_checkpoints
        with pytest.raises(ValueError, match="already exists"):
            await runner.run(dagent.ToolAgent(profile="conversation"), input="reuse", run_id="cancelled")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["initial", "resume"])
@pytest.mark.parametrize("failure", ["model", "storage", "delivery", "exception"])
async def test_all_root_failure_exits_evict_after_error_state_is_saved(tmp_path, monkeypatch, stage, failure):
    @dagent.tool(risk="medium")
    def record():
        return ToolOutput("binary", value=b"data") if failure == "storage" else "ok"

    class BrokenProvider(MockProvider):
        async def chat(self, *args, **kwargs):
            value = await super().chat(*args, **kwargs)
            if value.content == "raise":
                raise RuntimeError("provider broke")
            return value

    responses = [ChatResponse(tool_calls=[ToolCall(id="write", name="tool_record")]),
                 ChatResponse(content="raise") if failure == "exception" else ChatResponse()]
    with closing(dagent.Runner(provider=BrokenProvider(responses), workspace=tmp_path, skill_roots=[], max_retained_runs=0)) as runner:
        agent = dagent.ToolAgent(profile="conversation", capabilities=[record], review="careful" if stage == "resume" else "fast")
        first = await runner.run(agent, input="write", run_id="failed") if stage == "resume" else None
        if failure == "storage":
            def fail(*args, **kwargs):
                raise OSError("disk full")
            monkeypatch.setattr(result_storage, "_write_reference", fail)

        async def reject(batch):
            raise OSError("host rejected")

        if failure == "delivery":
            operation = (runner.resume_stream(first.review.approve(), checkpoint=first.checkpoint,
                         persistence=dagent.StreamPersistenceOptions()) if first is not None else
                         runner.stream(agent, input="write", run_id="failed", persistence=dagent.StreamPersistenceOptions()))
            events = []
            async for event in operation:
                events.append(event)
                if event.type == "conversation.changed":
                    await runner.reject_changes(event.run_id, event.data.batch_id, message="host rejected")
            assert events[-1].type == "run.failed"
            assert runner.run_state("failed") is None and runner.run_checkpoint("failed") is None
            return
        operation = (runner.resume(first.review.approve(), checkpoint=first.checkpoint) if first is not None else
                     runner.run(agent, input="write", run_id="failed"))
        if failure == "model":
            result = await operation
            assert result.status == "failed" and result.checkpoint is not None
        else:
            with pytest.raises((dagent.RunExecutionError, RuntimeError)):
                await operation
        assert runner.run_state("failed") is None
        assert runner.run_checkpoint("failed") is None
        assert not runner.runtime.session._review_index


@pytest.mark.asyncio
async def test_dag_agent_sessions_are_owned_and_evicted_without_external_stores(tmp_path):
    owned_agent = dagent.ToolAgent(profile="conversation", name="helper", capabilities=[])
    external_store = AgentNodeSessionStore()
    external_store.save(task_id="dag_0", node_id="external", fingerprint="f", conversation=dagent.ConversationState())
    provider = MockProvider([ChatResponse(content="answer")] * 4)
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[], max_retained_runs=1)) as runner:
        AgentCapabilityProvider({"external": {"provider": provider, "profile": "conversation"}},
            runtime_directory=".dagent", session_store=external_store).register_into(runner.runtime.capability_catalog)
        dag = dagent.Dag("sessions")
        dag.add_node(dagent.Node("helper", target=owned_agent, inputs={"prompt": "help"}))
        for index in range(4):
            result = await runner.run(dag, run_id=f"dag_{index}")
            assert result.status == "completed" and result.node_output("helper") == "answer"
            store = runner._agent_session_stores["helper"]
            assert {key[0] for key in store._conversations} == {f"dag_{index}"}
        assert external_store.get(task_id="dag_0", node_id="external", fingerprint="f") is not None


@pytest.mark.asyncio
async def test_static_child_retries_and_review_survive_other_root_evictions(tmp_path):
    effects = []

    @dagent.tool(risk="medium")
    def write() -> str:
        effects.append("written")
        return "ok"

    responses = [ChatResponse(), ChatResponse(tool_calls=[ToolCall(id="write", name="tool_write")]),
                 *[ChatResponse(content="other")] * 12, ChatResponse(), ChatResponse(content="done")]
    with closing(dagent.Runner(provider=MockProvider(responses), workspace=tmp_path, skill_roots=[], max_retained_runs=0)) as runner:
        child = dagent.ToolAgent(profile="conversation", name="helper", capabilities=[write], response_retries=1)
        graph = dagent.Dag("review")
        graph.add_node(dagent.Node("worker", target=child, inputs={"prompt": "write"}))
        pending = await runner.run(graph, review="careful", run_id="dag_review")
        assert pending.requires_review and effects == []
        store = runner._agent_session_stores["helper"]
        assert len(store._conversations) == 1
        for _ in range(12):
            await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[]), input="other")
        assert runner.run_checkpoint("dag_review") is not None
        saved = dagent.RunCheckpoint.model_validate_json(pending.checkpoint.model_dump_json())
        result = await runner.resume(pending.review.approve(), checkpoint=saved)
        assert result.status == "completed" and result.node_output("worker") == "done"
        assert effects == ["written"]
        assert not store._conversations and not runner.runtime.runs and not runner._run_checkpoints


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_compaction_failure_is_evicted_after_its_snapshot_is_saved(tmp_path, monkeypatch, resume):
    @dagent.tool(risk="medium")
    def record() -> str:
        return "ok"

    old = dagent.AssistantMessage(run_id="previous", content="old evidence " * 4000)
    policy = dagent.ContextPolicy(compaction_strategy="chunked", summary_max_tokens=512)
    # Prepared-call support is needed by this strategy; the endpoint supplies it
    # without making real network requests.
    def respond(request):
        body = json.loads(request.content)
        return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"), content="",
                           calls=[ToolCall(id="write", name="tool_record")])

    async with _endpoint(protocol="chat_completions", token_counting="heuristic", context_window_tokens=4096,
                         max_output_tokens=512, responder=respond) as (provider, _):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[], max_retained_runs=0)) as runner:
            agent = dagent.ToolAgent(profile="conversation", capabilities=[record], review="careful", context=policy)
            if resume:
                first = await runner.run(agent, input="write", run_id="compact")
                saved = first.checkpoint.model_copy(deep=True)
                for field in ("conversation", "model_thread"):
                    thread = getattr(saved.state, field)
                    if thread is not None:
                        setattr(saved.state, field, thread.model_copy(update={"items": (old, *thread.items)}))
            def fail_archive(self, *args, **kwargs):
                raise CompactionArchiveError("archive unavailable")
            monkeypatch.setattr(HistoryCompactor, "_archive", fail_archive)
            with pytest.raises(RuntimeError):
                if resume:
                    await runner.resume(first.review.approve(), checkpoint=saved)
                else:
                    await runner.run(agent, input="write", run_id="compact", conversation=dagent.ConversationState(items=(old,)))
            assert runner.run_state("compact") is None and runner.run_checkpoint("compact") is None
            assert not runner.runtime.session._review_index


@pytest.mark.asyncio
async def test_eviction_preserves_conversation_and_compaction_archives(tmp_path):
    @dagent.tool
    def query() -> str:
        return "evidence " * 200

    turns = 0
    def respond(request):
        nonlocal turns
        body = json.loads(request.content)
        text = str(body["messages"])
        if "Sources to summarize:" in text or body["messages"][0]["content"].startswith("Produce a task checkpoint"):
            aliases = request_aliases(text)
            content = checkpoint(requirements=[("Keep evidence.", [aliases[0]])], next_step=[("Continue.", [aliases[-1]])])
            return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"), content=content)
        turns += 1
        return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"), content="done" if turns > 20 else "",
                           calls=() if turns > 20 else [ToolCall(id=f"call_{turns}", name="tool_query")])

    async with _endpoint(protocol="chat_completions", token_counting="heuristic", context_window_tokens=8192,
                         max_output_tokens=512, responder=respond) as (provider, _):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[], max_retained_runs=0)) as runner:
            agent = dagent.ToolAgent(profile="conversation", capabilities=[query],
                context=dagent.ContextPolicy(compaction_strategy="chunked", summary_max_tokens=512))
            result = await runner.run(agent, input="Keep evidence.")
            assert result.status == "completed" and result.conversation.summary is not None
            archive = runner.resolve_runtime_path(result.conversation.summary.compaction_record.path,
                                                  workspace_path=result.workspace_path)
            before = archive.read_bytes()
            assert runner.run_state(result.run_id) is None
            continued = await runner.run(agent, input="continue", conversation=result.conversation)
            assert continued.status == "completed" and archive.read_bytes() == before


@pytest.mark.asyncio
async def test_released_results_and_completed_run_heap_reach_a_plateau(tmp_path):
    class FreshProvider(MockProvider):
        async def chat(self, *args, **kwargs):
            return ChatResponse(content=("0123456789" * 12000).encode().decode())

    with closing(dagent.Runner(provider=FreshProvider(), workspace=tmp_path, skill_roots=[], max_retained_runs=2)) as runner:
        agent = dagent.ToolAgent(profile="conversation", capabilities=[])
        samples = []
        refs = []
        tracemalloc.start()
        try:
            for index in range(16):
                result = await runner.run(agent, input="large response", run_id=f"heap_{index}")
                refs.append(weakref.ref(runner.runtime.runs[result.run_id]))
                del result
                gc.collect()
                samples.append(tracemalloc.get_traced_memory()[0])
                assert len(runner.runtime.runs) == min(index + 1, 2)
            assert all(ref() is None for ref in refs[:-2])
            # Allow allocator/test overhead and the deliberately retained ids.
            assert max(samples[8:]) - min(samples[8:]) < 200_000
        finally:
            tracemalloc.stop()


@pytest.mark.asyncio
async def test_unlimited_retention_keeps_an_unsaved_failed_run_id_retryable(tmp_path):
    class FailingOnce(MockProvider):
        failed = False
        async def chat(self, *args, **kwargs):
            if not self.failed:
                self.failed = True
                raise RuntimeError("network down")
            return await super().chat(*args, **kwargs)

    with closing(dagent.Runner(provider=FailingOnce([ChatResponse(content="done")]),
                               workspace=tmp_path, skill_roots=[])) as runner:
        agent = dagent.ToolAgent(profile="conversation")
        with pytest.raises(RuntimeError, match="network down"):
            await runner.run(agent, input="hello", run_id="retry")
        assert runner.run_state("retry") is None and not runner._used_run_ids
        result = await runner.run(agent, input="hello", run_id="retry")
        assert result.status == "completed"
        with pytest.raises(ValueError, match="already exists"):
            await runner.run(agent, input="again", run_id="retry")


@pytest.mark.asyncio
async def test_unlimited_retention_blocks_a_released_persistent_review_id(tmp_path):
    from tests.test_conversation_changes import Host, invoke

    @dagent.tool(risk="medium")
    def gate() -> str:
        return "ok"

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="gate", name="tool_gate")]), ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        agent = dagent.ToolAgent(profile="conversation", capabilities=[gate], review="careful")
        pending, _ = await invoke(runner, agent, Host(runner=runner), True, input="review", run_id="review")
        assert pending.status == "awaiting_review" and runner.run_state("review") is None
        with pytest.raises(ValueError, match="already exists"):
            await runner.run(agent, input="reuse", run_id="review")
