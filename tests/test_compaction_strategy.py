from __future__ import annotations

import json
import re
import asyncio
from types import SimpleNamespace

import pytest

from dagent.harness_runtime.compaction import (
    CompactionArchiveError, CompactionBudgetExceeded, CompactionChunk, CompactionError,
    CompactionRecordV1, CompactionRunState, CompactionSourceV1, HistoryCompactor,
    SummaryEntry, TaskSummary, load_compaction_references,
)
from dagent.harness_runtime.context import ContextAssembler
from dagent.harness_runtime.result_storage import ResultStore
from dagent.providers.base import ToolCall
from dagent.providers.model_io import (
    ModelAssistantTurn, ModelRequest, ModelResponse, ModelToolResultInput, ModelUserInput,
    ModelTokenCount,
)
from dagent.providers import ProviderReprepareRequired
from dagent.schemas.context import ContextPolicy, ModelCallMetadata, ModelTokenUsage
from dagent.schemas.conversation import (
    AssistantMessage, ContextSummary, FrozenResultDisplay, ToolCallItem,
    ToolResultMessage, UserMessage, inline_content,
)


def summary_response(request: ModelRequest) -> ModelResponse:
    text = request.items[-1].content
    if "Eligible source IDs:" in text:
        ids = json.loads(re.search(r"Eligible source IDs: (.+)\n", text).group(1))
    elif request.items[0].source_id == "compaction_chunks":
        checkpoints = json.loads(text.split("\n", 1)[1])
        ids = [entry["source_ids"][0] for checkpoint in checkpoints
               for entries in checkpoint.values() for entry in entries]
    else:
        ids = re.findall(r"\[Source ([^;]+);", text)
    return ModelResponse(content=json.dumps({
        "requirements": [{"text": "Preserve the user requirement.", "source_ids": [ids[0]]}],
        "next_step": [{"text": "Continue pending work.", "source_ids": [ids[-1]]}],
    }))


def compactor(tmp_path, chat, *, strategy="chunked", window=4096,
              readable=True, fallback=False, timeout=None):
    return HistoryCompactor(
        policy=ContextPolicy(compaction_strategy=strategy,
                             allow_reference_fallback=fallback,
                             compaction_timeout_seconds=timeout),
        assembler=ContextAssembler(context_window_tokens=window),
        result_store=ResultStore(tmp_path, ".runtime"),
        system_message={"content": "Original system."}, chat=chat,
        read_available=readable,
    )


def record_for(engine, summary):
    return CompactionRecordV1.model_validate_json(
        engine.result_store.read_reference(summary.compaction_record))


@pytest.mark.asyncio
async def test_chunking_covers_middle_and_all_source_ranges(tmp_path):
    requests = []

    async def chat(request):
        requests.append(request)
        return summary_response(request)

    item = UserMessage(id="original", content="a" * 6000 + "MIDDLE_REQUIREMENT" + "z" * 6000)
    engine = compactor(tmp_path, chat, window=4096)
    summary = await engine.compact(None, (item,), 256)
    record = record_for(engine, summary)
    assert record.status == "complete"
    assert record.unresolved_ranges == ()
    assert len(record.chunks) > 1
    assert any("MIDDLE_REQUIREMENT" in request.items[0].content for request in requests)
    ranges = [span for chunk in record.chunks for span in chunk.ranges]
    assert ranges[0].start == 0
    assert ranges[-1].end == len("User:\n" + item.content)
    assert all(left.end == right.start for left, right in zip(ranges, ranges[1:]))
    assert not summary.source_truncated and not summary.output_truncated
    assert all(request.strict_context_budget for request in requests)
    assert engine.logical_calls <= 8


@pytest.mark.asyncio
async def test_cache_aware_preserves_background_prefix_and_excludes_latest_group(tmp_path):
    requests = []

    async def chat(request):
        requests.append(request)
        return summary_response(request)

    old = AssistantMessage(id="completed", content="An earlier step is finished.")
    background = ModelUserInput(source_id="task", content="Current task must remain.")
    old_request = ModelAssistantTurn(source_id=old.id, content=old.content)
    latest = ModelAssistantTurn(source_id="latest", tool_calls=(ToolCall(id="last", name="query"),))
    result = ModelToolResultInput(source_id="result", call_id="last", name="query", content="LATEST_BODY")
    tools = ({"type": "function", "function": {"name": "query"}},)
    engine = compactor(tmp_path, chat, strategy="cache_aware")
    original = ModelRequest(instructions="Original system.", items=(background, old_request, latest, result), tools=tools)
    engine.source_request = original
    summary = await engine.compact(None, (old,), 256)
    request = requests[0]
    assert len(requests) == 1
    assert request.instructions == original.instructions
    assert request.tools == tools
    assert request.items[:-1] == (background, old_request)
    assert "LATEST_BODY" not in str(request)
    assert "background only" in request.items[-1].content
    archive = CompactionSourceV1.model_validate_json(
        engine.result_store.read_reference(record_for(engine, summary).source))
    assert [item.id for item in archive.items] == [old.id]


@pytest.mark.asyncio
async def test_invalid_prefix_falls_back_once_to_isolated_compactor(tmp_path):
    requests = []

    async def chat(request):
        requests.append(request)
        return ModelResponse(content="wrong format") if len(requests) == 1 else summary_response(request)

    item = UserMessage(id="source", content="Keep this requirement.")
    engine = compactor(tmp_path, chat, strategy="cache_aware")
    engine.source_request = ModelRequest(instructions="Original system.", items=(ModelUserInput(source_id=item.id, content=item.content),))
    summary = await engine.compact(None, (item,), 256)
    assert summary.method == "model"
    assert [call.kind for call in record_for(engine, summary).calls] == ["prefix", "chunk"]
    assert requests[1].instructions != requests[0].instructions


@pytest.mark.asyncio
async def test_archive_omits_reasoning_and_preserves_received_snapshot_and_view(tmp_path):
    async def chat(request):
        return summary_response(request)

    engine = compactor(tmp_path, chat)
    recovery = engine.result_store.snapshot_text("COMPLETE_RESULT_BODY")
    items = (
        AssistantMessage(id="call", reasoning="PRIVATE_REASONING", tool_calls=(ToolCallItem(id="call-id", name="query"),)),
        ToolResultMessage(id="result", call_id="call-id", name="query", status="completed",
                          content=inline_content("COMPLETE_RESULT_BODY"),
                          model_display=FrozenResultDisplay(text="Shown: COMPLETE", recovery=recovery, displayed_spans=((0, 8),))),
    )
    summary = await engine.compact(None, items, 256)
    record = record_for(engine, summary)
    raw = engine.result_store.read_reference(record.source)
    assert b"PRIVATE_REASONING" not in raw
    assert b"COMPLETE_RESULT_BODY" in raw
    assert b'"displayed_spans":[[0,8]]' in raw
    assert recovery in load_compaction_references(raw)
    assert record.source in load_compaction_references(engine.result_store.read_reference(summary.compaction_record))


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    ModelResponse(content=""),
    ModelResponse(content="{}"),
    ModelResponse(content='{"requirements":[{"text":"invented","source_ids":["unknown"]}]}'),
    ModelResponse(content="text", tool_calls=(ToolCall(id="never", name="shell"),)),
    ModelResponse(content="text", refusal="No."),
    ModelResponse(content="text", status="incomplete"),
    ModelResponse(content="text", metadata=ModelCallMetadata(protocol="chat_completions", finish_reason="length")),
])
async def test_invalid_summary_never_replaces_history(tmp_path, response):
    async def chat(_request):
        return response

    engine = compactor(tmp_path, chat)
    item = UserMessage(id="source", content="Keep the original.")
    with pytest.raises(CompactionError):
        await engine.compact(None, (item,), 256)
    assert item.content == "Keep the original."
    assert engine.logical_calls == 1


@pytest.mark.asyncio
async def test_reference_fallback_is_explicit_truthful_and_model_free(tmp_path):
    async def chat(_request):
        raise AssertionError("Memo fallback must not make a model request")

    engine = compactor(tmp_path, chat, fallback=True)
    item = UserMessage(id="source", content="Complete source with a middle requirement.")
    summary = await engine.reference_fallback(None, (item,), 256, reason="previous attempt failed")
    record = record_for(engine, summary)
    assert summary.method == "reference_fallback"
    assert "[DEGRADED_CONTEXT]" in summary.content
    assert record.status == "reference_only"
    assert record.unresolved_ranges == record.source_ranges
    assert record.calls == ()
    assert summary.source_truncated is False
    assert summary.usage is None and summary.model_call is None
    assert engine.result_store.read_reference(record.source)


@pytest.mark.asyncio
async def test_fallback_requires_read_capability_and_successful_storage(tmp_path, monkeypatch):
    async def chat(_request):
        return ModelResponse(content="invalid")

    item = UserMessage(id="source", content="Original")
    engine = compactor(tmp_path, chat, fallback=True, readable=False)
    with pytest.raises(CompactionArchiveError, match="read capability"):
        await engine.compact(None, (item,), 256, allow_fallback=True)

    def fail(_text):
        raise OSError("disk failed")

    monkeypatch.setattr(engine.result_store, "snapshot_text", fail)
    with pytest.raises(CompactionArchiveError, match="archive"):
        await engine.compact(None, (item,), 256, allow_fallback=True)


@pytest.mark.asyncio
async def test_partial_fallback_records_success_and_unprocessed_ranges(tmp_path):
    calls = 0

    async def chat(request):
        nonlocal calls
        calls += 1
        return summary_response(request) if calls == 1 else ModelResponse(content="invalid")

    engine = compactor(tmp_path, chat, window=4096, fallback=True)
    item = UserMessage(id="source", content="a" * 14000)
    summary = await engine.compact(None, (item,), 256, allow_fallback=True)
    record = record_for(engine, summary)
    assert record.status == "partial"
    assert len(record.chunks) == 1
    assert record.unresolved_ranges
    assert record.unresolved_ranges[0].start == record.chunks[0].ranges[-1].end
    assert "Preserve the user requirement" in summary.content


@pytest.mark.asyncio
async def test_http_and_deadline_budgets_cover_whole_episode(tmp_path):
    async def chat(request):
        return summary_response(request)

    engine = compactor(tmp_path, chat)
    item = UserMessage(id="source", content="Original")
    engine.http_attempts = 12
    with pytest.raises(CompactionBudgetExceeded, match="request budget"):
        await engine.compact(None, (item,), 256)
    engine.reset_budget()
    engine.deadline = 0
    with pytest.raises(CompactionBudgetExceeded, match="deadline"):
        await engine.compact(None, (item,), 256)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,transport", [
    (ConnectionError("connection reset"), True),
    (TimeoutError("read timed out"), True),
    (ValueError("request rejected"), False),
])
async def test_summary_transport_failures_are_marked_transient(tmp_path, error, transport):
    async def chat(request):
        raise error

    engine = compactor(tmp_path, chat)
    with pytest.raises(CompactionError) as failed:
        await engine.compact(None, (UserMessage(id="source", content="Original"),), 256)
    # The released error type and reason prefix stay unchanged.
    assert type(failed.value) is CompactionError
    assert failed.value.transient is transport
    assert record_for(engine, failed.value).status == "reference_only"


@pytest.mark.asyncio
async def test_unreachable_warm_prefix_does_not_retry_through_chunks(tmp_path):
    requests = []

    async def chat(request):
        requests.append(request)
        raise ConnectionError("connection refused")

    engine = compactor(tmp_path, chat, strategy="cache_aware")
    engine.source_request = ModelRequest(instructions="Original system.", items=(
        ModelUserInput(source_id="u", content="Requirement"), ModelAssistantTurn(source_id="a", content="Done")))
    with pytest.raises(CompactionError) as failed:
        await engine.compact(None, (UserMessage(id="u", content="Requirement"), AssistantMessage(id="a", content="Done")), 256)
    assert failed.value.transient
    assert len(requests) == 1 and requests[0].items[-1].source_id == "compaction_control"


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy,kind", [("cache_aware", "prefix"), ("chunked", "chunk"), ("chunked", "reduce")])
async def test_summary_requests_state_the_visible_budget(tmp_path, strategy, kind):
    requests = []

    async def chat(request):
        requests.append(request)
        return summary_response(request)

    items = (UserMessage(id="u", content="Requirement"), AssistantMessage(id="a", content="Done"))
    engine = compactor(tmp_path, chat, strategy=strategy)
    if kind == "prefix":
        engine.source_request = ModelRequest(instructions="Original system.", items=(
            ModelUserInput(source_id="u", content="Requirement"),
            ModelAssistantTurn(source_id="a", content="Done")))
        await engine.compact(None, items, 500)
    elif kind == "chunk":
        await engine.compact(None, items, 500)
    else:
        chunks = [CompactionChunk(ranges=(), summary=TaskSummary(next_step=(SummaryEntry(text=text, source_ids=("u",)),)))
                  for text in ("First", "Second")]
        await engine._reduce(chunks, {"u": "Requirement"}, 500, None)
    text = requests[0].instructions + "".join(getattr(item, "content", "") for item in requests[0].items)
    assert "must stay under 400 tokens (about 1600 English characters or 400 CJK characters)" in text
    assert "cite that checkpoint's source ID instead of IDs shown inside it" in text


@pytest.mark.asyncio
async def test_carried_forward_citations_resolve_to_the_previous_checkpoint(tmp_path, caplog):
    previous = ContextSummary(content="### Completed Work\n- Read the config [msg_old1, msg_old2]",
                              source_item_count=2)
    item = UserMessage(id="source", content="Now update the config.")

    async def chat(request):
        return ModelResponse(content=json.dumps({
            "completed_work": [{"text": "Read the config.", "source_ids": ["msg_old1", "msg_old2"]}],
            "next_step": [{"text": "Update it.", "source_ids": ["source", "msg_typo"]}],
        }))

    engine = compactor(tmp_path, chat)
    summary = await engine.compact(previous, (item,), 256)
    checkpoint = record_for(engine, summary).task_summary
    assert checkpoint.completed_work[0].source_ids == ("previous_summary",)
    assert checkpoint.next_step[0].source_ids == ("source",)
    assert "[previous_summary]" in summary.content and "msg_typo" not in summary.content

    async def invented(request):
        return ModelResponse(content=json.dumps({"next_step": [{"text": "Update it.", "source_ids": ["msg_typo"]}]}))

    with pytest.raises(CompactionError, match="unknown source"):
        await compactor(tmp_path, invented).compact(previous, (item,), 256)
    # Dropping an unknown ID is reported, never silent.
    assert "cited 1 unknown source IDs; they were removed" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("recovers", [True, False])
async def test_oversized_summary_is_retried_once_with_its_measured_size(tmp_path, recovers):
    requests = []

    async def chat(request):
        requests.append(request)
        text = "Short checkpoint." if recovers and len(requests) > 1 else "x" * 4000
        return ModelResponse(content=json.dumps({"next_step": [{"text": text, "source_ids": ["source"]}]}))

    engine = compactor(tmp_path, chat)
    item = UserMessage(id="source", content="Original")
    if recovers:
        summary = await engine.compact(None, (item,), 256)
        calls = record_for(engine, summary).calls
        assert [call.error is not None for call in calls] == [True, False]
        assert "Short checkpoint." in summary.content
    else:
        with pytest.raises(CompactionError, match="visible output budget") as failed:
            await engine.compact(None, (item,), 256)
        assert type(failed.value) is CompactionError and failed.value.oversized_tokens == 1007
    assert len(requests) == 2
    # The note is an instruction; the summarized source data is unchanged.
    assert "Your previous checkpoint was about 1007 tokens; the limit is 256." in requests[1].instructions
    assert requests[1].items == requests[0].items


@pytest.mark.asyncio
async def test_oversized_warm_prefix_retry_extends_only_its_control_turn(tmp_path):
    requests = []

    async def chat(request):
        requests.append(request)
        text = "Short checkpoint." if len(requests) > 1 else "x" * 4000
        return ModelResponse(content=json.dumps({"next_step": [{"text": text, "source_ids": ["a"]}]}))

    engine = compactor(tmp_path, chat, strategy="cache_aware")
    engine.source_request = ModelRequest(instructions="Original system.", items=(
        ModelUserInput(source_id="u", content="Requirement"), ModelAssistantTurn(source_id="a", content="Done")))
    await engine.compact(None, (UserMessage(id="u", content="Requirement"), AssistantMessage(id="a", content="Done")), 256)
    assert [request.items[-1].source_id for request in requests] == ["compaction_control"] * 2
    assert requests[1].items[:-1] == requests[0].items[:-1]
    assert requests[1].instructions == requests[0].instructions
    assert "Your previous checkpoint was about" in requests[1].items[-1].content


@pytest.mark.asyncio
async def test_oversized_retry_is_skipped_when_the_shorter_request_cannot_fit(tmp_path):
    requests = []

    async def chat(request):
        requests.append(request)
        return ModelResponse(content=json.dumps({"next_step": [{"text": "x" * 4000, "source_ids": ["source"]}]}))

    engine = compactor(tmp_path, chat)
    fits = engine._fits

    async def no_room_for_note(request):
        return "Your previous checkpoint" not in request.instructions and await fits(request)

    engine._fits = no_room_for_note
    with pytest.raises(CompactionError, match="visible output budget"):
        await engine.compact(None, (UserMessage(id="source", content="Original"),), 256)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cached,switches", [(100, True), (900, False), (None, False)])
async def test_low_prefix_cache_hits_switch_the_run_to_chunk_summaries(tmp_path, cached, switches):
    from dataclasses import replace

    async def chat(request):
        usage = ModelTokenUsage(input_tokens=1000, cached_input_tokens=cached)
        return replace(summary_response(request), usage=usage)

    monitor = CompactionRunState()
    kinds = []
    for index in range(3):
        items = (UserMessage(id=f"u{index}", content="Requirement"), AssistantMessage(id=f"a{index}", content="Done"))
        engine = compactor(tmp_path, chat, strategy="cache_aware")
        engine.run_state = monitor
        engine.source_request = ModelRequest(instructions="Original system.", items=(
            ModelUserInput(source_id=f"u{index}", content="Requirement"),
            ModelAssistantTurn(source_id=f"a{index}", content="Done")))
        summary = await engine.compact(None, items, 256)
        kinds.append([call.kind for call in record_for(engine, summary).calls])
    # Two consecutive low hits switch; unknown cache usage never decides.
    assert monitor.prefix_disabled is switches
    assert kinds == [["prefix"], ["prefix"], ["chunk"] if switches else ["prefix"]]


def test_one_low_prefix_hit_or_an_unsent_root_does_not_switch():
    monitor = CompactionRunState()
    low, high = (ModelTokenUsage(input_tokens=1000, cached_input_tokens=value) for value in (100, 900))
    for usage in (low, high, low, None, high, low):
        monitor.observe_prefix(usage)
    # A single eviction between good hits never disables the warm prefix.
    assert not monitor.prefix_disabled


@pytest.mark.asyncio
async def test_prefix_after_a_new_root_summary_is_not_sampled(tmp_path):
    from dataclasses import replace

    async def chat(request):
        return replace(summary_response(request), usage=ModelTokenUsage(input_tokens=1000, cached_input_tokens=0))

    monitor = CompactionRunState()
    for index in range(3):
        engine = compactor(tmp_path, chat, strategy="cache_aware")
        engine.run_state = monitor
        # The caller marks prefixes whose root summary was replaced in this episode.
        engine.prefix_sample = False
        engine.source_request = ModelRequest(instructions="Original system.", items=(
            ModelUserInput(source_id=f"u{index}", content="Requirement"),
            ModelAssistantTurn(source_id=f"a{index}", content="Done")))
        await engine.compact(None, (UserMessage(id=f"u{index}", content="Requirement"),
                                    AssistantMessage(id=f"a{index}", content="Done")), 256)
    assert monitor.low_prefix_hits == 0 and not monitor.prefix_disabled


@pytest.mark.asyncio
async def test_previous_record_and_legacy_manifest_are_explicit_links(tmp_path):
    async def chat(request):
        return summary_response(request)

    engine = compactor(tmp_path, chat)
    first = await engine.compact(None, (UserMessage(id="first", content="First requirement"),), 256)
    legacy_result = ToolResultMessage(call_id="old-call", name="query", status="completed", content=inline_content("old result"))
    manifest = engine.result_store.snapshot_text(legacy_result.model_dump_json() + "\n")
    first = first.model_copy(update={"result_manifest": manifest})
    second = await engine.compact(first, (UserMessage(id="second", content="Second requirement"),), 256)
    record = record_for(engine, second)
    source = CompactionSourceV1.model_validate_json(engine.result_store.read_reference(record.source))
    assert record.previous_record == first.compaction_record
    assert source.previous_result_manifest == manifest
    assert record.prior_coverage == "known"
    assert second.source_item_count == 2


@pytest.mark.asyncio
async def test_failed_record_reuses_partial_chunks_without_new_model_calls(tmp_path):
    calls = 0

    async def chat(request):
        nonlocal calls
        calls += 1
        return summary_response(request) if calls == 1 else ModelResponse(content="invalid")

    item = UserMessage(id="source", content="x" * 14000)
    engine = compactor(tmp_path, chat, window=4096, fallback=True)
    with pytest.raises(CompactionError) as failure:
        await engine.compact(None, (item,), 256)
    failed_ref = failure.value.compaction_record
    failed = CompactionRecordV1.model_validate_json(engine.result_store.read_reference(failed_ref))
    assert failed.status == "partial"
    assert len(failed.chunks) == 1
    assert failed.calls[-1].error
    attempts = calls
    engine.reset_budget()
    summary = await engine.reference_fallback(None, (item,), 256, reason="cached failure", failed_record=failed_ref)
    record = record_for(engine, summary)
    assert calls == attempts
    assert record.chunks == failed.chunks
    assert record.reused_from == failed_ref
    assert record.calls == ()
    assert record.unresolved_ranges == failed.unresolved_ranges


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_fallback", [False, True])
@pytest.mark.parametrize("failure_mode", ["repeated_fingerprint", "limit"])
@pytest.mark.parametrize("via_context", [False, True])
async def test_count_reprepare_failure_preserves_paid_chunks_and_typed_signal(tmp_path, allow_fallback, failure_mode, via_context):
    calls = 0
    preparations = 0
    failed_counts = 0
    error = ProviderReprepareRequired(ValueError("explicit rejection"),
        metadata=ModelCallMetadata(protocol="chat_completions"))

    async def chat(request):
        nonlocal calls
        calls += 1
        return summary_response(request)

    async def prepare(request, *, stream):
        nonlocal preparations
        preparations += 1
        payload = {"messages": [request.items[0].content]}
        if failure_mode == "limit":
            payload["revision"] = preparations
        return SimpleNamespace(request=request, payload=payload, fingerprint=json.dumps(payload),
                               effective_max_output_tokens=request.max_output_tokens)

    async def count(call):
        nonlocal failed_counts
        if call.request.purpose == "generation":
            return ModelTokenCount(count=6000, exact=True)
        if calls:
            failed_counts += 1
            raise error
        return None

    item = UserMessage(id="source", run_id="old", content="x" * 14000)
    engine = compactor(tmp_path, chat, window=4096, fallback=True)
    engine.assembler.request_preparer = prepare
    engine.assembler.prepared_token_counter = count
    with pytest.raises(ProviderReprepareRequired) as failure:
        if via_context:
            from dagent.schemas import ConversationState

            await engine.assembler.prepare(
                system_message=engine.system_message,
                conversation=ConversationState(items=(item, UserMessage(run_id="active", content="Continue"))),
                active_run_id="active", policy=engine.policy, enable_new_strategy=True,
                result_store=engine.result_store, history_compactor=engine,
            )
        else:
            await engine.compact(None, (item,), 256, allow_fallback=allow_fallback)
    assert failure.value is error
    assert calls == 1
    assert failed_counts == (5 if failure_mode == "limit" else 1)
    reference = error.compaction_record
    if via_context:
        assert error._conversation.compaction_state.failed_records[0].record == reference
    record = CompactionRecordV1.model_validate_json(engine.result_store.read_reference(reference))
    assert record.status == "partial"
    assert record.source == error.source_reference
    assert len(record.chunks) == len(record.calls) == 1
    assert record.unresolved_ranges
    assert "explicit rejection" in record.failure_reason
    engine.reset_budget()
    recovered = await engine.reference_fallback(None, (item,), 256,
        reason="reuse paid work", failed_record=reference)
    recovered_record = record_for(engine, recovered)
    assert recovered_record.chunks == record.chunks
    assert recovered_record.reused_from == reference
    assert calls == 1


@pytest.mark.asyncio
async def test_tightening_count_reprepare_failure_retains_the_root_record(tmp_path):
    async def chat(request):
        return summary_response(request)

    engine = compactor(tmp_path, chat, fallback=True)
    original = await engine.compact(None, (UserMessage(id="source", content="Original requirement"),), 256)
    error = ProviderReprepareRequired(ValueError("explicit rejection"),
        metadata=ModelCallMetadata(protocol="chat_completions"))

    async def prepare(request, *, stream):
        return SimpleNamespace(request=request, payload={"messages": ["same"]}, fingerprint="same",
                               effective_max_output_tokens=request.max_output_tokens)

    async def count(call):
        raise error

    engine.assembler.request_preparer = prepare
    engine.assembler.prepared_token_counter = count
    with pytest.raises(ProviderReprepareRequired) as failure:
        await engine.tighten(original, 128, allow_fallback=True)
    assert failure.value is error
    record = CompactionRecordV1.model_validate_json(engine.result_store.read_reference(error.compaction_record))
    assert record.source == error.source_reference
    assert record.previous_record == original.compaction_record
    assert record.chunks == ()


@pytest.mark.asyncio
async def test_failed_record_with_changed_root_keeps_evidence_without_stale_chunks(tmp_path):
    calls = 0
    failing = True

    async def chat(request):
        nonlocal calls
        calls += 1
        if failing and calls > 1:
            return ModelResponse(content="invalid")
        return summary_response(request)

    item = UserMessage(id="old", content="old evidence " * 2000)
    engine = compactor(tmp_path, chat, fallback=True)
    with pytest.raises(CompactionError) as caught:
        await engine.compact(None, (item,), 256)
    failed_ref = caught.value.compaction_record
    failed = CompactionRecordV1.model_validate_json(engine.result_store.read_reference(failed_ref))
    assert failed.chunks

    failing = False
    engine.reset_budget()
    current = await engine.compact(None, (UserMessage(id="new", content="New root."),), 256)
    paid_calls = calls
    engine.reset_budget()
    recovered = await engine.reference_fallback(current, (item,), 256,
        reason="old source already attempted", failed_record=failed_ref)
    record = record_for(engine, recovered)
    assert calls == paid_calls
    assert record.reused_from == failed_ref
    assert record.previous_record == current.compaction_record
    assert record.chunks == ()
    assert record.unresolved_ranges == record.source_ranges
    assert recovered.method == "reference_fallback"
    assert "[DEGRADED_CONTEXT]" in recovered.content


@pytest.mark.asyncio
async def test_complete_paid_record_reuse_requires_exact_source_and_runs_without_fallback(tmp_path):
    calls = 0

    async def chat(request):
        nonlocal calls
        calls += 1
        return summary_response(request)

    engine = compactor(tmp_path, chat)
    item = UserMessage(id="source", content="Important evidence.")
    first = await engine.compact(None, (item,), 256)
    second = await engine.reuse_compaction(None, (item,), 256,
        record_reference=first.compaction_record)
    assert second == first
    assert calls == 1
    assert await engine.reuse_compaction(None,
        (item.model_copy(update={"content": "Changed evidence."}),), 256,
        record_reference=first.compaction_record) is None
    assert calls == 1


@pytest.mark.asyncio
async def test_missing_raw_recovery_reference_prevents_normal_compaction(tmp_path):
    async def chat(_request):
        raise AssertionError("Missing raw result must be found before a model call")

    engine = compactor(tmp_path, chat)
    reference = engine.result_store.snapshot_text("original tool output")
    (engine.result_store.workspace / reference.path).unlink()
    result = ToolResultMessage(call_id="missing", name="query", status="completed", content=reference)
    with pytest.raises(CompactionArchiveError, match="retain"):
        await engine.compact(None, (result,), 256)


@pytest.mark.asyncio
async def test_shared_deadline_bounds_token_preparation_and_model_request(tmp_path):
    async def slow_chat(_request):
        await asyncio.sleep(1)

    engine = compactor(tmp_path, slow_chat, timeout=0.01)
    item = UserMessage(id="source", content="Original")
    with pytest.raises(CompactionBudgetExceeded, match="deadline") as failure:
        await engine.compact(None, (item,), 256)
    assert failure.value.compaction_record is not None
    assert engine.logical_calls == 1

    async def slow_prepare(_request, *, stream):
        await asyncio.sleep(1)

    engine.reset_budget()
    engine.assembler.request_preparer = slow_prepare
    with pytest.raises(CompactionBudgetExceeded, match="deadline"):
        await engine.compact(None, (item,), 256)
    assert engine.logical_calls == 0


@pytest.mark.asyncio
async def test_final_prepared_wire_is_counted_and_reused_for_dispatch(tmp_path):
    prepared_calls = []
    counted = []

    async def prepare(request, *, stream):
        assert stream is True
        call = SimpleNamespace(request=request, payload={"messages": [request.items[0].content]},
                               effective_max_output_tokens=request.max_output_tokens)
        prepared_calls.append(call)
        return call

    async def count(call):
        counted.append(call)
        return ModelTokenCount(count=100, max_model_len=4096, exact=True)

    async def chat(request):
        assert engine.prepared_request is request
        assert engine.prepared_call in counted
        return summary_response(request)

    engine = compactor(tmp_path, chat)
    engine.assembler.request_preparer = prepare
    engine.assembler.prepared_token_counter = count
    summary = await engine.compact(None, (UserMessage(id="source", content="Requirement"),), 256)
    assert summary.method == "model"
    assert len(prepared_calls) == 1
    assert len(counted) == 1
    assert all(call is prepared_calls[0] for call in counted)


@pytest.mark.asyncio
async def test_inherited_references_remain_explicit_after_successful_later_summary(tmp_path):
    async def chat(request):
        return summary_response(request)

    engine = compactor(tmp_path, chat, fallback=True)
    first = await engine.reference_fallback(None, (UserMessage(id="old", content="Unavailable semantic history"),), 256, reason="offline")
    second = await engine.compact(first, (UserMessage(id="new", content="Continue the task"),), 256)
    assert second.method == "model"
    assert "[DEGRADED_CONTEXT]" in second.content
    assert record_for(engine, second).has_unresolved_history
    assert engine.assembler.token_counter.count_text(second.content) <= 256


@pytest.mark.asyncio
async def test_previous_summary_id_does_not_overwrite_a_user_supplied_id(tmp_path):
    async def chat(request):
        return summary_response(request)

    engine = compactor(tmp_path, chat)
    old = ContextSummary(content="Old requirement", source_item_count=1)
    summary = await engine.compact(old, (UserMessage(id="previous_summary", content="New requirement"),), 256)
    record = record_for(engine, summary)
    assert len(record.source_ranges) == 2
    assert {span.source_id for span in record.source_ranges} == {"previous_summary", "_previous_summary"}
    assert record.has_unresolved_history  # Legacy coverage is unknown.


@pytest.mark.asyncio
async def test_operation_evidence_is_typed_and_links_only_real_sources(tmp_path):
    async def chat(_request):
        return ModelResponse(content=json.dumps({"operation_evidence_refs": [{
            "text": "Query returned successfully; the remaining page uses its stored cursor.",
            "source_ids": ["result"],
        }]}))

    engine = compactor(tmp_path, chat)
    item = ToolResultMessage(id="result", call_id="call", name="query", status="completed", content=inline_content("Evidence"))
    summary = await engine.compact(None, (item,), 256)
    record = record_for(engine, summary)
    assert record.task_summary.operation_evidence_refs[0].source_ids == (item.id,)
    assert record.operation_ids == (item.call_id,)


@pytest.mark.asyncio
async def test_root_tightening_once_preserves_source_chain_without_raw_replay(tmp_path):
    requests = []

    async def chat(request):
        requests.append(request)
        return summary_response(request)

    engine = compactor(tmp_path, chat)
    first = await engine.compact(None, (UserMessage(id="raw", content="RAW_NOT_TO_REPLAY"),), 256)
    second = await engine.tighten(first, 128)
    reused = await engine.reuse_compaction(first, (), 128, record_reference=second.compaction_record)
    assert reused == second
    assert len(requests) == 2
    assert "RAW_NOT_TO_REPLAY" not in requests[-1].items[0].content
    assert first.content in requests[-1].items[0].content
    assert second.source_item_count == first.source_item_count
    record = record_for(engine, second)
    assert record.previous_record == first.compaction_record
    assert record.calls[0].kind == "reduce"
    assert engine.logical_calls == 2
    with pytest.raises(CompactionBudgetExceeded, match="already attempted"):
        await engine.tighten(second, 128)
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_tightening_keeps_inherited_degradation_and_failed_source_checkpoint(tmp_path):
    async def chat(request):
        return summary_response(request)

    engine = compactor(tmp_path, chat, fallback=True)
    previous = await engine.reference_fallback(None, (UserMessage(id="raw", content="Original history"),), 256, reason="offline")
    summary = await engine.tighten(previous, 128)
    assert "[DEGRADED_CONTEXT]" in summary.content
    assert record_for(engine, summary).has_unresolved_history
    engine.reset_budget()

    async def invalid(_request):
        return ModelResponse(content="invalid")

    engine.chat = invalid
    before = summary.model_dump_json()
    with pytest.raises(CompactionError) as failure:
        await engine.tighten(summary, 128)
    record = CompactionRecordV1.model_validate_json(engine.result_store.read_reference(failure.value.compaction_record))
    assert record.previous_record == summary.compaction_record
    assert summary.model_dump_json() == before


@pytest.mark.asyncio
async def test_compactor_persists_a_discovered_smaller_server_window(tmp_path):
    async def prepare(request, *, stream):
        return SimpleNamespace(request=request, payload={"messages": [request.items[0].content]},
                               effective_max_output_tokens=request.max_output_tokens)

    async def count(_call):
        return ModelTokenCount(count=100, max_model_len=4096, exact=True)

    async def chat(request):
        return summary_response(request)

    engine = compactor(tmp_path, chat, window=16384)
    engine.assembler.request_preparer = prepare
    engine.assembler.prepared_token_counter = count
    await engine.compact(None, (UserMessage(id="source", content="Requirement"),), 256)
    assert engine.assembler.server_max_model_len == 4096
    assert engine.assembler.context_window_tokens == 4096


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["invalid", "budget"])
async def test_root_only_hard_failure_can_use_explicit_reference_fallback(tmp_path, failure_kind):
    requests = []

    async def chat(request):
        requests.append(request)
        return summary_response(request) if len(requests) == 1 else ModelResponse(content="invalid")

    engine = compactor(tmp_path, chat, fallback=True)
    original = await engine.compact(None, (UserMessage(id="raw", content="Complete original evidence"),), 256)
    before = original.model_dump_json()
    if failure_kind == "budget":
        engine.logical_calls = 8
    summary = await engine.tighten(original, 128, allow_fallback=True)
    assert summary.method == "reference_fallback"
    assert summary.source_item_count == original.source_item_count
    assert original.model_dump_json() == before
    assert len(requests) == (1 if failure_kind == "budget" else 2)
    record = record_for(engine, summary)
    assert record.previous_record == original.compaction_record
    assert record.reused_from is not None
    failed = CompactionRecordV1.model_validate_json(engine.result_store.read_reference(record.reused_from))
    assert failed.unresolved_ranges == failed.source_ranges
    if failure_kind == "invalid":
        assert "invalid task summary" in failed.calls[-1].error


@pytest.mark.asyncio
async def test_root_reference_fallback_does_not_fake_read_access_or_metadata_room(tmp_path):
    async def chat(_request):
        return ModelResponse(content="invalid")

    engine = compactor(tmp_path, chat, fallback=True, readable=False)
    original = ContextSummary(content="Current summary that must remain intact.", source_item_count=1)
    with pytest.raises(CompactionArchiveError, match="read capability") as failure:
        await engine.tighten(original, 128, allow_fallback=True)
    assert failure.value.compaction_record is not None
    engine.reset_budget()
    engine.read_available = True
    with pytest.raises(CompactionBudgetExceeded, match="metadata"):
        await engine.tighten(original, 8, allow_fallback=True)
