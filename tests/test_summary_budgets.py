"""Budget regressions: count retained requests and reserve usable summary output."""

import math
import json
import re
from contextlib import closing
from types import SimpleNamespace

import pytest

import dagent
from dagent.harness_runtime.compaction import (
    CompactionArchiveError, CompactionError, CompactionSourceV1, HistoryCompactor,
    _CompactionCapacityUnavailable,
)
from dagent.harness_runtime.context import ContextAssembler
from dagent.harness_runtime.result_storage import ResultStore
from dagent.providers.model_io import ModelRequest, ModelTokenCount, ModelUserInput
from tests.test_compaction_strategy import record_for, summary_response


class RecordingCompactor:
    def __init__(self):
        self.goals = []
        self.sources = []

    async def compact(self, previous, items, max_tokens, *, allow_fallback=False):
        self.goals.append(max_tokens)
        self.sources.append(tuple(item.id for item in items))
        return dagent.ContextSummary(
            content="Keep the requirement and continue pending work.",
            source_item_count=(previous.source_item_count if previous else 0) + len(items),
        )


def history(*, separated=False):
    items = [dagent.UserMessage(id="task", run_id="run", content="Keep the requirement."),
             dagent.AssistantMessage(id="old", run_id="run", content="evidence " * 1600)]
    if separated:
        items.extend([dagent.UserMessage(id="steer", run_id="run", content="Keep this correction."),
                      dagent.AssistantMessage(id="other", run_id="run", content="more evidence " * 1600)])
    items.append(dagent.AssistantMessage(id="latest", run_id="run", content="Latest protected step."))
    return dagent.ConversationState(items=tuple(items))


@pytest.mark.asyncio
async def test_large_protected_remainder_does_not_collapse_summary_to_256():
    probes = []

    async def count(request, _field):
        ids = {item.source_id for item in request.items}
        probes.append(ids)
        return ModelTokenCount(count=20000 + 20000 * ("old" in ids)
                               + 100 * ("context_summary" in ids), exact=True)

    compactor = RecordingCompactor()
    prepared = await ContextAssembler(context_window_tokens=32768, request_token_counter=count).prepare(
        system_message={"content": "Inspect evidence."}, conversation=history(),
        policy=dagent.ContextPolicy(compaction_strategy="chunked"),
        history_compactor=compactor, enable_new_strategy=True,
    )
    assert compactor.goals == [1792]
    assert {"task", "latest"} in probes
    assert prepared.usage.estimated_input_tokens <= prepared.usage.input_budget_tokens


@pytest.mark.asyncio
async def test_separated_spans_can_make_progress_before_the_whole_request_fits():
    async def count(request, _field):
        ids = {item.source_id for item in request.items}
        return ModelTokenCount(count=18000 + 12000 * len(ids & {"old", "other"})
                               + 100 * ("context_summary" in ids), exact=True)

    compactor = RecordingCompactor()
    prepared = await ContextAssembler(context_window_tokens=32768, request_token_counter=count).prepare(
        system_message={"content": "Inspect evidence."}, conversation=history(separated=True),
        policy=dagent.ContextPolicy(compaction_strategy="chunked"),
        history_compactor=compactor, enable_new_strategy=True,
    )
    assert compactor.sources == [("old",), ("other",)]
    assert compactor.goals == [1792, 1792]
    assert [item.id for item in prepared.conversation.items] == ["task", "steer", "latest"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [64, 128, 512])
async def test_explicit_small_summary_limits_remain_supported(limit):
    async def count(request, _field):
        ids = {item.source_id for item in request.items}
        return ModelTokenCount(count=5000 + 5000 * ("old" in ids), exact=True)

    compactor = RecordingCompactor()
    await ContextAssembler(context_window_tokens=8192, request_token_counter=count).prepare(
        system_message={"content": "Inspect evidence."}, conversation=history(),
        policy=dagent.ContextPolicy(compaction_strategy="chunked", summary_max_tokens=limit),
        history_compactor=compactor, enable_new_strategy=True,
    )
    assert compactor.goals == [limit]


@pytest.mark.asyncio
async def test_no_capacity_does_not_call_summarizer_or_persist_failed_sources():
    async def count(request, _field):
        return ModelTokenCount(count=9000, exact=True)

    compactor = RecordingCompactor()
    with pytest.raises(dagent.ContextWindowExceeded) as failure:
        await ContextAssembler(context_window_tokens=8192, request_token_counter=count).prepare(
            system_message={"content": "Mandatory input."}, conversation=history(),
            policy=dagent.ContextPolicy(compaction_strategy="chunked"),
            history_compactor=compactor, enable_new_strategy=True,
        )
    assert not compactor.goals
    assert failure.value._conversation.compaction_state is None


@pytest.mark.asyncio
@pytest.mark.parametrize("window,goal,output", [(4096, 768, 1024), (8192, 1536, 2048),
                                               (8192, 256, 768), (32768, 512, 1536),
                                               (32768, 1792, 3584), (131072, 8192, 16384)])
async def test_summary_requests_reserve_output_and_bound_visible_target(tmp_path, window, goal, output):
    requests = []

    async def chat(request):
        requests.append(request)
        return summary_response(request)

    engine = HistoryCompactor(
        policy=dagent.ContextPolicy(compaction_strategy="chunked"),
        assembler=ContextAssembler(context_window_tokens=window),
        result_store=ResultStore(tmp_path, ".runtime"), system_message={"content": "Task"}, chat=chat,
    )
    await engine.compact(None, (dagent.UserMessage(id="source", content="Preserve this requirement."),), goal)
    assert requests[0].max_output_tokens == output
    assert f"under {math.floor(.8 * goal)} tokens" in requests[0].instructions


def prepared_engine(tmp_path, *, window=32768, prefix_input=100, override=None, discovered=None):
    sent, counted = [], []

    async def chat(request):
        sent.append(request)
        return summary_response(request)

    async def prepare(request, *, stream):
        assert stream
        output = override if override is not None else request.max_output_tokens
        return SimpleNamespace(request=request, effective_max_output_tokens=output,
                               payload={"prompt": request.instructions, "output": output},
                               fingerprint=f"{request.instructions}:{output}")

    async def count(call):
        counted.append(call)
        prefix = call.request.items[-1].source_id == "compaction_control"
        return ModelTokenCount(count=prefix_input if prefix else 100, exact=True,
                               max_model_len=discovered, source="render")

    assembler = ContextAssembler(context_window_tokens=window, request_preparer=prepare,
                                 prepared_token_counter=count)
    engine = HistoryCompactor(policy=dagent.ContextPolicy(compaction_strategy="cache_aware"),
        assembler=assembler, result_store=ResultStore(tmp_path, ".runtime"),
        system_message={"content": "Task"}, chat=chat)
    engine.source_request = ModelRequest(instructions="Original instructions.", items=(
        ModelUserInput(source_id="source", content="Preserve the requirement."),))
    return engine, sent, counted


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix_input,is_prefix,output", [(29400, True, 3368), (30100, False, 4096)])
async def test_prefix_reduces_only_spare_output_then_uses_chunks(tmp_path, prefix_input, is_prefix, output):
    engine, sent, counted = prepared_engine(tmp_path, prefix_input=prefix_input)
    original = engine.source_request
    await engine.compact(None, (dagent.UserMessage(id="source", content="Preserve the requirement."),), 2048)
    assert len(sent) == 1
    request = sent[0]
    assert (request.items[-1].source_id == "compaction_control") is is_prefix
    assert request.max_output_tokens == output
    text = request.instructions + request.items[-1].content
    assert "under 1638 tokens" in text  # The visible target was never sacrificed for caching.
    if is_prefix:
        assert request.items[:-1] == original.items
        assert request.instructions == original.instructions
        assert len(counted) == 2


@pytest.mark.asyncio
async def test_small_effective_override_rebuilds_the_prompt_and_validation_budget(tmp_path):
    engine, sent, _ = prepared_engine(tmp_path, override=512)
    await engine.compact(None, (dagent.UserMessage(id="source", content="Requirement."),), 2048)
    assert "under 307 tokens" in sent[0].items[-1].content  # 384 visible + 128 reserve.
    assert engine.prepared_call.effective_max_output_tokens == 512

    async def oversized(request):
        from tests.test_compaction_strategy import checkpoint, request_aliases
        from dagent.providers.model_io import ModelResponse
        ids = request_aliases(request.items[-1].content)
        return ModelResponse(content=checkpoint(next_step=[("word " * 350, [ids[-1]])]))

    engine.reset_budget()
    engine.chat = oversized
    with pytest.raises(CompactionError, match="visible output budget"):
        await engine.compact(None, (dagent.UserMessage(id="source", content="Requirement."),), 2048)
    assert all("visible output budget" in call.error for call in engine.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["compact", "tighten"])
@pytest.mark.parametrize("enabled,allow_fallback,readable", [
    (False, True, True), (True, False, True), (True, True, False),
])
async def test_preflight_capacity_fallback_requires_authorization_and_recovery(
    tmp_path, operation, enabled, allow_fallback, readable,
):
    engine, sent, _ = prepared_engine(tmp_path, override=64)
    engine.policy = engine.policy.model_copy(update={"allow_reference_fallback": enabled})
    engine.read_available = readable
    original = dagent.ContextSummary(content="Preserve the current checkpoint.", source_item_count=1)
    error = CompactionArchiveError if enabled and allow_fallback else _CompactionCapacityUnavailable
    with pytest.raises(error) as failure:
        if operation == "compact":
            await engine.compact(None, (dagent.UserMessage(id="source", content="Requirement."),),
                                 256, allow_fallback=allow_fallback)
        else:
            await engine.tighten(original, 256, allow_fallback=allow_fallback)
    assert failure.value.compaction_record is None
    assert not sent and engine.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["chunked", "cache_aware"])
@pytest.mark.parametrize("root_only", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
async def test_preflight_output_override_preserves_reference_continuation(
    tmp_path, strategy, root_only, fallback,
):
    from tests.test_context_soak import READ_TOOL
    from tests.test_reasoning_mapping import _endpoint

    async def chat(_request):
        pytest.fail("A 64-token output override must skip model summarization.")

    source = history()
    if root_only:
        source = source.model_copy(update={
            "summary": dagent.ContextSummary(content="Earlier evidence. " * 1000, source_item_count=1),
            "items": (source.items[0], source.items[-1]),
        })
    policy = dagent.ContextPolicy(compaction_strategy=strategy, allow_reference_fallback=fallback)
    store = ResultStore(tmp_path, ".runtime")
    async with _endpoint(protocol="chat_completions", token_counting="heuristic",
                         context_window_tokens=4096,
                         extra_request_args={"extra_body": {"max_completion_tokens": 64}}) as (provider, posts):
        assembler = ContextAssembler(context_window_tokens=4096, request_preparer=provider.prepare_call,
                                     prepared_token_counter=provider.count_prepared)
        engine = HistoryCompactor(policy=policy, assembler=assembler, result_store=store,
                                  system_message={"content": "Task"}, chat=chat, read_available=True)
        arguments = dict(system_message=engine.system_message, conversation=source, tools=[READ_TOOL],
                         policy=policy, result_store=store, history_compactor=engine, enable_new_strategy=True)
        if not fallback:
            with pytest.raises(dagent.ContextWindowExceeded) as failure:
                await assembler.prepare(**arguments)
            assert failure.value._conversation.compaction_state is None
            assert failure.value._conversation.items == source.items
            assert failure.value._conversation.summary == source.summary
        else:
            prepared = await assembler.prepare(**arguments)
            summary = prepared.conversation.summary
            assert summary.method == "reference_fallback"
            assert "[DEGRADED_CONTEXT]" in summary.content
            assert summary.source_item_count == 1
            assert prepared.conversation.compaction_state is None
            assert prepared.conversation.items == (source.items[0], source.items[-1])
            assert prepared.usage.estimated_input_tokens <= prepared.usage.input_budget_tokens
            assert prepared.prepared_call.effective_max_output_tokens == 64
            record = record_for(engine, summary)
            assert record.status == "reference_only"
            assert record.calls == () and record.reused_from is None
            assert record.unresolved_ranges == record.source_ranges
            archive = CompactionSourceV1.model_validate_json(store.read_reference(record.source))
            assert archive.previous_summary == (source.summary.content if root_only else None)
            assert archive.items == (() if root_only else (source.items[1],))
        assert posts == []
        assert engine.calls == []


@pytest.mark.asyncio
async def test_summary_discovered_window_shrink_replans_output_and_visible_goal(tmp_path):
    engine, sent, _ = prepared_engine(tmp_path, discovered=8192)
    await engine.compact(None, (dagent.UserMessage(id="source", content="Requirement."),), 8192)
    assert len(sent) == 1
    assert sent[0].max_output_tokens == 2048
    assert "under 1228 tokens" in sent[0].items[-1].content
    assert engine.assembler.context_window_tokens == 8192


@pytest.mark.asyncio
async def test_reprepared_summary_uses_its_new_visible_limit_for_validation_and_retry(tmp_path):
    from dagent.providers.model_io import ModelResponse
    from tests.test_compaction_strategy import checkpoint, request_aliases

    engine, _, _ = prepared_engine(tmp_path)
    native_prepare = engine.assembler.request_preparer
    reduced = False
    requests = []

    async def prepare(request, *, stream):
        call = await native_prepare(request, stream=stream)
        if reduced:
            call.effective_max_output_tokens = 512
            call.payload = {**call.payload, "output": 512}
            call.fingerprint = json.dumps(call.payload)
        return call

    async def chat(request):
        nonlocal reduced
        requests.append(request)
        if len(requests) == 1:
            reduced = True
            replacement = await engine.reprepare()
            assert "under 307 tokens" in replacement.request.items[-1].content
            ids = request_aliases(replacement.request.items[-1].content)
            return ModelResponse(content=checkpoint(next_step=[("word " * 350, [ids[-1]])]))
        return summary_response(request)

    engine.assembler.request_preparer = prepare
    engine.chat = chat
    await engine.compact(None, (dagent.UserMessage(id="source", content="Requirement."),), 2048)
    assert len(requests) == 2
    assert "the limit is 384" in requests[1].items[-1].content
    assert engine.calls[0].error == "Compactor summary exceeds its visible output budget."


@pytest.mark.asyncio
async def test_planning_request_output_override_does_not_replace_generation_budget():
    counts = []

    async def prepare(request, *, stream):
        has_old = any(item.source_id == "old" for item in request.items)
        return SimpleNamespace(request=request, effective_max_output_tokens=2048 if has_old else 4096,
                               payload={"messages": [item.content for item in request.items]}, fingerprint=str(has_old))

    async def count(call):
        ids = {item.source_id for item in call.request.items}
        counts.append(ids)
        return ModelTokenCount(count=20000 + 20000 * ("old" in ids), exact=True)

    compactor = RecordingCompactor()
    prepared = await ContextAssembler(context_window_tokens=32768, request_preparer=prepare,
                                      prepared_token_counter=count).prepare(
        system_message={"content": "Task"}, conversation=history(),
        policy=dagent.ContextPolicy(compaction_strategy="chunked"),
        history_compactor=compactor, enable_new_strategy=True,
    )
    assert compactor.goals == [1920]  # Planned against the real 2048 generation allowance.
    assert prepared.usage.max_output_tokens == 4096  # The replacement gets its own final check.
    assert len(counts) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("exact", [False, True])
async def test_retained_request_margin_is_applied_only_to_estimates(exact):
    async def count(request, _field):
        ids = {item.source_id for item in request.items}
        return ModelTokenCount(count=15000 + 15000 * ("old" in ids), exact=exact)

    compactor = RecordingCompactor()
    await ContextAssembler(context_window_tokens=32768, request_token_counter=count).prepare(
        system_message={"content": "Inspect evidence."}, conversation=history(),
        policy=dagent.ContextPolicy(compaction_strategy="chunked"),
        history_compactor=compactor, enable_new_strategy=True,
    )
    if exact:
        assert 3000 < compactor.goals[0] < 3200
    else:
        assert compactor.goals == [1792]


@pytest.mark.asyncio
async def test_capacity_skip_can_retry_after_new_result_display_is_shortened(tmp_path):
    from dagent.providers.model_io import ModelToolResultInput
    from dagent.schemas.conversation import inline_content
    from tests.test_context_soak import READ_TOOL

    async def count(request, _field):
        ids = {item.source_id for item in request.items}
        body = any(isinstance(item, ModelToolResultInput) and "Ω" in item.content for item in request.items)
        return ModelTokenCount(count=1000 + 8000 * ("old" in ids) + 8000 * body, exact=True)

    source = history().model_copy(update={"items": (*history().items[:-1],
        dagent.AssistantMessage(id="call", run_id="run", tool_calls=(dagent.ToolCallItem(id="q", name="query"),)),
        dagent.ToolResultMessage(id="result", run_id="run", call_id="q", name="query",
                                 status="completed", content=inline_content("Ω" * 12000)),
    )})
    compactor = RecordingCompactor()
    prepared = await ContextAssembler(context_window_tokens=8192, request_token_counter=count).prepare(
        system_message={"content": "Inspect evidence."}, conversation=source, tools=[READ_TOOL],
        result_store=ResultStore(tmp_path, ".runtime"),
        policy=dagent.ContextPolicy(compaction_strategy="chunked"),
        history_compactor=compactor, enable_new_strategy=True,
    )
    assert compactor.sources == [("old",)]
    assert prepared.conversation.compaction_state is None
    assert prepared.usage.estimated_input_tokens <= prepared.usage.input_budget_tokens
    assert [item.id for item in prepared.conversation.items] == ["task", "call", "result"]


@pytest.mark.asyncio
async def test_small_root_can_tighten_in_a_large_window():
    class RootCompactor(RecordingCompactor):
        async def tighten(self, summary, max_tokens, *, allow_fallback=False):
            self.goals.append(max_tokens)
            return summary.model_copy(update={"content": "x" * (4 * max_tokens)})

    async def count(request, _field):
        text = next((item.content for item in request.items if item.source_id == "context_summary"), "")
        return ModelTokenCount(count=113800 + math.ceil(len(text) / 4), exact=True)

    compactor = RootCompactor()
    source = dagent.ConversationState(summary=dagent.ContextSummary(content="x" * 4000, source_item_count=3),
        items=(dagent.UserMessage(id="task", run_id="run", content="Keep the requirement."),
               dagent.AssistantMessage(id="latest", run_id="run", content="Latest step.")))
    prepared = await ContextAssembler(context_window_tokens=131072, request_token_counter=count).prepare(
        system_message={"content": "Inspect evidence."}, conversation=source,
        policy=dagent.ContextPolicy(compaction_strategy="chunked"),
        history_compactor=compactor, enable_new_strategy=True,
    )
    assert len(compactor.goals) == 1
    assert 600 < compactor.goals[0] < 800
    assert prepared.usage.estimated_input_tokens <= prepared.usage.input_budget_tokens


@pytest.mark.asyncio
@pytest.mark.parametrize("window,parallel,words", [(8192, 1, 180), (32768, 4, 1800), (131072, 8, 1800)])
async def test_endurance_with_substantial_checkpoints_and_enforced_reasoning_output(
    tmp_path, window, parallel, words,
):
    from dagent.harness_runtime.context import HeuristicTokenCounter
    from dagent.providers import ToolCall
    from tests.test_cache_usage import _completion, _usage
    from tests.test_compaction_strategy import checkpoint, request_aliases
    from tests.test_reasoning_mapping import _endpoint

    step = 0
    summaries = []
    truncated = []
    executions = []
    counter = HeuristicTokenCounter()

    @dagent.tool
    def query(index: int) -> str:
        """Collect one immutable page of evidence."""
        executions.append(index)
        return f"Evidence {index}; invariant=amber42. " + "evidence " * words

    def respond(request):
        nonlocal step
        body = json.loads(request.content)
        messages = body["messages"]
        text = "\n".join(message.get("content") or "" for message in messages)
        summary = "Sources to summarize:" in text or messages[0].get("content", "").startswith("Produce a task checkpoint")
        if summary:
            ids = request_aliases(text)
            target = int(re.findall(r"must stay under (\d+) tokens", text)[-1])
            detail = "Verified observation. " * max(0, (target * 4 - 500) // 22)
            content = checkpoint(requirements=[("Keep invariant amber42.", [ids[0]])],
                                 completed_work=[("Earlier evidence was collected. " + detail, [ids[-1]])],
                                 next_step=[("Continue from the latest result without repeating operations.", [ids[-1]])])
            limit = body.get("max_completion_tokens", body.get("max_tokens"))
            reasoning = "r" * (4 * min(900, window // 16))
            incomplete = counter.count_text(content) + counter.count_text(reasoning) > limit
            summaries.append((target, limit))
            if incomplete:
                truncated.append(limit)
            return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"),
                               content=content, reasoning=reasoning,
                               status="incomplete" if incomplete else "completed")
        assert "amber42" in text
        step += 1
        calls = () if step > 25 else tuple(ToolCall(id=f"call_{step}_{i}", name="tool_query",
            arguments={"index": (step - 1) * parallel + i}) for i in range(parallel))
        return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"),
                           content="done" if step > 25 else "", calls=calls)

    async with _endpoint(protocol="chat_completions", token_counting="heuristic",
                         context_window_tokens=window, responder=respond) as (provider, _posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            agent = dagent.ToolAgent(profile="conversation", capabilities=[query], max_steps=30,
                                    context=dagent.ContextPolicy(compaction_strategy="cache_aware"))
            result = await runner.run(agent, input="Collect 25 batches; keep invariant amber42.",
                                      workspace_path=tmp_path / "run")
    assert result.status == "completed"
    assert executions == list(range(25 * parallel))
    assert len(summaries) >= 2
    assert not truncated
    assert all(limit <= window // 4 for _, limit in summaries)
    assert all(usage.estimated_input_tokens <= usage.input_budget_tokens for usage in result.context_usage)
    assert "amber42" in result.conversation.summary.content
    assert counter.count_text(result.conversation.summary.content) > 256
    assert result.conversation.compaction_state is None
