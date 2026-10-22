from __future__ import annotations

import json
import re
import asyncio
from types import SimpleNamespace

import pytest

import dagent.harness_runtime.compaction as compaction
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


def request_aliases(text: str) -> list[str]:
    """Aliases offered by any summary request: prefix, chunk or reduce."""
    line = re.search(r"Sources to summarize: ([^\n]+)", text)
    if line:
        return [token.strip() for token in line.group(1).split(",")]
    found = re.findall(r"\[Source (S\d+);", text)
    if not found:
        found = [token.strip() for group in re.findall(r"\[(S\d+(?:, S\d+)*)\]", text)
                 for token in group.split(",")]
    return list(dict.fromkeys(found))


def checkpoint(**sections: list[tuple[str, list[str]]]) -> str:
    """Render a Markdown checkpoint with the supplied evidence citations."""
    return "\n".join(
        f"### {name.replace('_', ' ').title()}\n"
        + "\n".join(f"- {text} [{', '.join(ids)}]" for text, ids in entries)
        for name, entries in sections.items() if entries)


def summary_response(request: ModelRequest) -> ModelResponse:
    ids = request_aliases(request.items[-1].content)
    return ModelResponse(content=checkpoint(
        requirements=[("Preserve the user requirement.", [ids[0]])],
        next_step=[("Continue pending work.", [ids[-1]])],
    ))


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
    assert summary.content == record.task_summary.render()
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
    assert "Background messages give context only" in request.items[-1].content
    archive = CompactionSourceV1.model_validate_json(
        engine.result_store.read_reference(record_for(engine, summary).source))
    assert [item.id for item in archive.items] == [old.id]


@pytest.mark.asyncio
async def test_invalid_prefix_falls_back_once_to_isolated_compactor(tmp_path):
    requests = []

    async def chat(request):
        requests.append(request)
        return ModelResponse(content="wrong format") if len(requests) <= 2 else summary_response(request)

    item = UserMessage(id="source", content="Keep this requirement.")
    engine = compactor(tmp_path, chat, strategy="cache_aware")
    engine.source_request = ModelRequest(instructions="Original system.", items=(ModelUserInput(source_id=item.id, content=item.content),))
    summary = await engine.compact(None, (item,), 256)
    assert summary.method == "model"
    assert [call.kind for call in record_for(engine, summary).calls] == ["prefix", "prefix", "chunk"]
    assert requests[2].instructions != requests[0].instructions


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
    ModelResponse(content="### Next Step"),
    ModelResponse(content="- floating entry [S1]"),
    ModelResponse(content="### Next Step\n- invented [msg_unknown]"),
    ModelResponse(content="text", tool_calls=(ToolCall(id="never", name="shell"),)),
    ModelResponse(content="text", refusal="No."),
    ModelResponse(content="text", status="incomplete"),
    ModelResponse(content="text", metadata=ModelCallMetadata(protocol="chat_completions", finish_reason="length")),
    ModelResponse(content="text", metadata=ModelCallMetadata(protocol="chat_completions", finish_reason="max_tokens")),
    ModelResponse(content="text", metadata=ModelCallMetadata(protocol="responses", finish_reason="max_output_tokens")),
    ModelResponse(content="text", metadata=ModelCallMetadata(protocol="responses", response_status="incomplete")),
    ModelResponse(content="text", metadata=ModelCallMetadata(protocol="responses", response_status="failed")),
    ModelResponse(content="text", metadata=ModelCallMetadata(protocol="responses", response_status="cancelled")),
])
async def test_invalid_summary_never_replaces_history(tmp_path, response):
    async def chat(_request):
        return response

    engine = compactor(tmp_path, chat)
    item = UserMessage(id="source", content="Keep the original.")
    with pytest.raises(CompactionError):
        await engine.compact(None, (item,), 256)
    assert item.content == "Keep the original."
    terminal = response.tool_calls or response.refusal or response.status != "completed" or response.metadata
    assert engine.logical_calls == (1 if terminal else 2)


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
        await engine._reduce(chunks, {"u": "Requirement"}, 500, None, compaction._SourceAliases({"u": "Requirement"}, None))
    text = requests[0].instructions + "".join(getattr(item, "content", "") for item in requests[0].items)
    assert "must stay under 400 tokens (about 1600 English characters or 400 CJK characters)" in text
    assert "cite S0, never IDs shown inside it" in text


@pytest.mark.asyncio
async def test_carried_forward_citations_resolve_to_the_previous_checkpoint(tmp_path, caplog):
    previous = ContextSummary(content="### Completed Work\n- Read the config [msg_old1, msg_old2]",
                              source_item_count=2)
    item = UserMessage(id="source", content="Now update the config.")

    async def chat(request):
        return ModelResponse(content=checkpoint(completed_work=[("Read the config.", ["msg_old1", "msg_old2"])],
                                                next_step=[("Update it.", ["S1", "msg_typo"])]))

    engine = compactor(tmp_path, chat)
    summary = await engine.compact(previous, (item,), 256)
    task_summary = record_for(engine, summary).task_summary
    assert task_summary.completed_work[0].source_ids == ("previous_summary",)
    assert task_summary.next_step[0].source_ids == ("source",)
    assert "[previous_summary]" in summary.content and "msg_typo" not in summary.content

    async def invented(request):
        return ModelResponse(content=checkpoint(next_step=[("Update it.", ["msg_typo"])]))

    with pytest.raises(CompactionError, match="invalid task summary"):
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
        return ModelResponse(content=checkpoint(next_step=[(text, ["S1"])]))

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
        return ModelResponse(content=checkpoint(next_step=[(text, ["S2"])]))

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
        return ModelResponse(content=checkpoint(next_step=[("x" * 4000, ["S1"])]))

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
        return ModelResponse(content=checkpoint(operation_evidence_refs=[("Query returned successfully; the remaining page uses its stored cursor.", ["S1"])]))

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
    assert request_aliases(requests[-1].items[0].content) == ["S0"]
    assert second.source_item_count == first.source_item_count
    record = record_for(engine, second)
    assert record.previous_record == first.compaction_record
    assert record.calls[0].kind == "reduce"
    assert record.task_summary.requirements[0].source_ids == ("previous_summary",)
    assert record.task_summary.next_step[0].source_ids == ("previous_summary",)
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
    assert len(requests) == (1 if failure_kind == "budget" else 3)
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


def test_parse_checkpoint_all_sections_use_real_source_ids():
    aliases = compaction._SourceAliases({"previous_summary": "Earlier", "msg_a": "First", "msg_b": "Second"}, "previous_summary")
    text = checkpoint(**{name: [(f"Entry for {name}.", ["S0", "S1", "S2"])] for name in TaskSummary.model_fields})
    summary, report = compaction._parse_checkpoint(text, aliases, set())
    assert not any(report.values())
    for name in TaskSummary.model_fields:
        assert getattr(summary, name) == (SummaryEntry(text=f"Entry for {name}.",
                                                       source_ids=("previous_summary", "msg_a", "msg_b")),)
    assert [line for line in summary.render().splitlines() if line.startswith("###")] == [
        "### " + name.replace("_", " ").title() for name in TaskSummary.model_fields]


@pytest.mark.parametrize("prefix,suffix,ignored", [
    ("```markdown\n", "\n```", 0),
    ("Here is the checkpoint:\n", "\n\nEnd of checkpoint.", 2),
    ("<think>discard this</think>discard more</think>\n", "", 1),
    ("", "\nEnd of checkpoint.", 1),
])
def test_parse_checkpoint_tolerates_wrappers(prefix, suffix, ignored):
    aliases = compaction._SourceAliases({"msg_a": "First"}, None)
    summary, report = compaction._parse_checkpoint(prefix + "### Next Step\n- Continue. [S1]" + suffix, aliases, set())
    assert summary.next_step == (SummaryEntry(text="Continue.", source_ids=("msg_a",)),)
    assert report == {"unknown_headings": 0, "outside_sections": 0, "uncited": 0, "ignored_lines": ignored}


@pytest.mark.parametrize("bullet", ["-", "*", "•", "1.", "2)"])
def test_parse_checkpoint_accepts_bullet_variants(bullet):
    summary, report = compaction._parse_checkpoint(f"### Next Step\n{bullet} Continue. [S1]",
                                        compaction._SourceAliases({"msg_a": "First"}, None), set())
    assert summary.next_step[0].text == "Continue."
    assert not any(report.values())


def test_parse_checkpoint_joins_continuation_lines():
    summary, report = compaction._parse_checkpoint("### Completed Work\n- Read config.yaml\n  and confirmed timeout=30s [S1]",
                                        compaction._SourceAliases({"msg_a": "First"}, None), set())
    assert summary.completed_work[0].text == "Read config.yaml and confirmed timeout=30s"
    assert not any(report.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["chunked", "cache_aware"])
@pytest.mark.parametrize("continuation", ["Continue.", "Continue\n  pending work.", "Use list[int]\n  to continue."])
async def test_compaction_preserves_cited_entries_before_trailing_commentary(tmp_path, strategy, continuation):
    async def chat(_request):
        return ModelResponse(content=("### Requirements\n- Keep this requirement. [S1]\n"
                                      f"### Next Step\n- {continuation} [S1]\nEnd of checkpoint."))

    item = UserMessage(id="source", content="Keep this requirement and continue pending work.")
    engine = compactor(tmp_path, chat, strategy=strategy)
    engine.source_request = ModelRequest(instructions="Original system.", items=(
        ModelUserInput(source_id=item.id, content=item.content),))
    summary = await engine.compact(None, (item,), 256)
    record = record_for(engine, summary)
    expected = TaskSummary(
        requirements=(SummaryEntry(text="Keep this requirement.", source_ids=(item.id,)),),
        next_step=(SummaryEntry(text=" ".join(continuation.split()), source_ids=(item.id,)),),
    )
    assert record.status == "complete"
    assert record.task_summary == expected
    assert summary.content == expected.render()
    assert len(record.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["chunked", "cache_aware"])
async def test_compaction_preserves_literal_closing_think_tags_after_reasoning_normalization(tmp_path, strategy):
    command = "grep '</think>' app.log"

    async def chat(_request):
        return ModelResponse(content="<think>Private reasoning.</think>\n" + checkpoint(
            requirements=[(f"Run `{command}` exactly.", ["S1"])],
            next_step=[("Inspect the matches.", ["S1"])],
        ))

    item = UserMessage(id="source", content=f"Run `{command}` exactly and inspect the matches.")
    engine = compactor(tmp_path, chat, strategy=strategy)
    engine.source_request = ModelRequest(instructions="Original system.", items=(
        ModelUserInput(source_id=item.id, content=item.content),))
    summary = await engine.compact(None, (item,), 256)
    record = record_for(engine, summary)
    expected = TaskSummary(
        requirements=(SummaryEntry(text=f"Run `{command}` exactly.", source_ids=(item.id,)),),
        next_step=(SummaryEntry(text="Inspect the matches.", source_ids=(item.id,)),),
    )
    assert record.status == "complete"
    assert record.task_summary == expected
    assert summary.content == expected.render()
    assert len(record.calls) == 1


@pytest.mark.parametrize("heading,field", [
    ("## completed work", "completed_work"),
    ("Completed Work:", "completed_work"),
    ("**Next Step**", "next_step"),
    ("__Next Step__:", "next_step"),
    ("### **Confirmed-Decisions**:", "confirmed_decisions"),
])
def test_parse_checkpoint_accepts_heading_variants(heading, field):
    summary, report = compaction._parse_checkpoint(f"{heading}\n- Continue. [S1]",
                                        compaction._SourceAliases({"msg_a": "First"}, None), set())
    assert getattr(summary, field)[0].text == "Continue."
    assert not any(report.values())


def test_parse_checkpoint_discards_outside_entries_and_recovers_after_unknown_heading():
    text = ("- Floating. [S1]\n#### Checkpoint 1\n- Unknown section. [S1]\n"
            "### Next Step\n- Continue. [S1]\n### Not A Section\n- Discard this too. [S1]")
    summary, report = compaction._parse_checkpoint(text, compaction._SourceAliases({"msg_a": "First"}, None), set())
    assert summary.next_step[0].text == "Continue."
    assert report == {"unknown_headings": 2, "outside_sections": 3, "uncited": 0, "ignored_lines": 0}


def test_parse_checkpoint_only_uses_the_last_bracket_as_citations():
    text = "### Next Step\n- Use list[int] [S2]\n- Use list[int]\n- Keep the empty list []"
    summary, report = compaction._parse_checkpoint(text, compaction._SourceAliases({"msg_a": "First", "msg_b": "Second"}, None), set())
    assert summary.next_step == (SummaryEntry(text="Use list[int]", source_ids=("msg_b",)),)
    assert report["uncited"] == 2


def test_parse_checkpoint_resolves_alias_variants_and_preserves_real_and_inherited_ids():
    sources = {"previous_summary": "### Completed Work\n- Earlier. [msg_old]", "msg_a": "First", "msg_b": "Second"}
    aliases = compaction._SourceAliases(sources, "previous_summary")
    inherited = compaction._inherited_ids(sources["previous_summary"])
    summary, report = compaction._parse_checkpoint("### Next Step\n- Continue. [s1, S01, msg_b, msg_old, s00]", aliases, inherited)
    assert inherited == {"msg_old"}
    assert compaction._inherited_ids(None) == set()
    assert summary.next_step[0].source_ids == ("msg_a", "msg_b", "msg_old", "previous_summary")
    normalized = compaction._normalize_citations(summary, sources, "previous_summary")
    assert normalized.next_step[0].source_ids == ("msg_a", "msg_b", "previous_summary")
    assert not any(report.values())


@pytest.mark.parametrize("source_id", ["S1", "s01", "S0", "s00"])
@pytest.mark.parametrize("inherited", [False, True])
def test_checkpoint_aliases_do_not_shadow_real_or_inherited_ids(source_id, inherited):
    sources = {"msg_first": "First"}
    previous_id = "previous_summary" if inherited else None
    if inherited:
        sources = {previous_id: checkpoint(completed_work=[("Earlier.", [source_id])]), **sources}
    else:
        sources[source_id] = "Second"
    aliases = compaction._SourceAliases(sources, previous_id)
    summary, report = compaction._parse_checkpoint(
        checkpoint(next_step=[("Continue.", [source_id])]), aliases,
        compaction._inherited_ids(sources.get(previous_id)),
    )
    assert summary.next_step[0].source_ids == (source_id,)
    normalized = compaction._normalize_citations(summary, sources, previous_id)
    assert normalized.next_step[0].source_ids == (previous_id if inherited else source_id,)
    assert all(aliases.resolve(aliases.alias(key)) == key for key in sources)
    assert not any(report.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["chunked", "cache_aware"])
@pytest.mark.parametrize("inherited", [False, True])
async def test_compaction_records_alias_like_real_citations_without_reassigning_sources(tmp_path, strategy, inherited):
    async def chat(_request):
        return ModelResponse(content=checkpoint(
            requirements=[("Keep the first requirement.", ["msg_first"])],
            next_step=[("Continue the cited task.", ["S1"])],
        ))

    previous = ContextSummary(content=checkpoint(completed_work=[("Earlier.", ["S1"])]),
                              source_item_count=1) if inherited else None
    items = (UserMessage(id="msg_first", content="Keep the first requirement."),)
    if not inherited:
        items += (UserMessage(id="S1", content="Continue the cited task."),)
    engine = compactor(tmp_path, chat, strategy=strategy)
    prefix = (ModelUserInput(source_id="context_summary", content=previous.content),) if previous else ()
    engine.source_request = ModelRequest(instructions="Original system.", items=(
        *prefix, *(ModelUserInput(source_id=item.id, content=item.content) for item in items)))
    summary = await engine.compact(previous, items, 256)
    record = record_for(engine, summary)
    expected = TaskSummary(
        requirements=(SummaryEntry(text="Keep the first requirement.", source_ids=("msg_first",)),),
        next_step=(SummaryEntry(text="Continue the cited task.",
                               source_ids=("previous_summary" if inherited else "S1",)),),
    )
    assert record.status == "complete"
    assert record.task_summary == expected
    for entry in (*expected.requirements, *expected.next_step):
        assert f"{entry.text} [{', '.join(entry.source_ids)}]" in summary.content
    assert len(record.calls) == 1


def test_parse_checkpoint_leaves_unknown_tokens_for_normalization(caplog):
    sources = {"msg_a": "First"}
    summary, report = compaction._parse_checkpoint("### Next Step\n- Continue. [S99, S1, msg_typo]", compaction._SourceAliases(sources, None), set())
    assert summary.next_step[0].source_ids == ("S99", "msg_a", "msg_typo")
    assert not any(report.values())
    normalized = compaction._normalize_citations(summary, sources, None)
    assert normalized.next_step[0].source_ids == ("msg_a",)
    assert "cited 2 unknown source IDs" in caplog.text
    with pytest.raises(CompactionError, match="unknown source"):
        compaction._normalize_citations(TaskSummary(next_step=(SummaryEntry(text="Invalid.", source_ids=("S99",)),)), sources, None)


@pytest.mark.parametrize("text", [
    "", "{}", "garbled", "### Next Step", "- Floating. [S1]", "### Unknown\n- Discard. [S1]",
    "### Next Step\n- Use list[int]", "### Next Step\n- Invented. [S99]", "### Next Step\n-   [S1]",
])
def test_parse_checkpoint_without_usable_entries_is_malformed(text):
    with pytest.raises(CompactionError, match=r"^Compactor returned an invalid task summary\.$") as failure:
        compaction._parse_checkpoint(text, compaction._SourceAliases({"msg_a": "First"}, None), set())
    assert failure.value.malformed is True


def test_parse_checkpoint_report_contains_only_counts():
    text = ("PRIVATE preamble\n### Unknown\n- PRIVATE outside [S1]\n"
            "### Next Step\n- PRIVATE uncited\n\nPRIVATE ignored\n- Continue. [S1]")
    _, report = compaction._parse_checkpoint(text, compaction._SourceAliases({"msg_a": "First"}, None), set())
    assert report == {"unknown_headings": 1, "outside_sections": 1, "uncited": 1, "ignored_lines": 2}
    assert all(type(value) is int for value in report.values())
    assert "PRIVATE" not in str(report)


def test_render_with_aliases_retains_unknown_real_ids():
    summary = TaskSummary(next_step=(SummaryEntry(text="Continue.", source_ids=("msg_a", "msg_external")),))
    assert compaction._render_with_aliases(summary, compaction._SourceAliases({"msg_a": "First"}, None)) == (
        "### Next Step\n- Continue. [S1, msg_external]")


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["chunked", "cache_aware"])
@pytest.mark.parametrize("invalid", ['{"next_step": [{"text": "Continue.", "source_ids": ["source"]}]}', "garbled"])
async def test_malformed_checkpoint_retries_in_instructions_and_records_both_calls(tmp_path, strategy, invalid):
    requests = []

    async def chat(request):
        requests.append(request)
        return ModelResponse(content=invalid) if len(requests) == 1 else summary_response(request)

    item = UserMessage(id="source", content="Keep this requirement.")
    engine = compactor(tmp_path, chat, strategy=strategy)
    engine.source_request = ModelRequest(instructions="Original system.", items=(ModelUserInput(source_id=item.id, content=item.content),))
    summary = await engine.compact(None, (item,), 256)
    calls = record_for(engine, summary).calls
    assert len(requests) == len(calls) == 2
    assert [call.error is not None for call in calls] == [True, False]
    assert "invalid task summary" in calls[0].error
    note = "Your previous output did not follow the checkpoint format."
    if strategy == "chunked":
        assert requests[1].items == requests[0].items
        assert note in requests[1].instructions
    else:
        assert requests[1].items[:-1] == requests[0].items[:-1]
        assert requests[1].instructions == requests[0].instructions
        assert requests[1].items[-1].source_id == "compaction_control"
        assert requests[1].items[-1].content.startswith(requests[0].items[-1].content + "\n" + note)


@pytest.mark.asyncio
async def test_malformed_checkpoint_fails_after_one_retry_and_keeps_reason_prefix(tmp_path):
    from dagent.schemas import ConversationState

    async def chat(_request):
        return ModelResponse(content="garbled")

    engine = compactor(tmp_path, chat)
    source = UserMessage(id="source", run_id="old", content="Evidence " * 1200)
    with pytest.raises(CompactionError, match="invalid task summary") as failure:
        await engine.compact(None, (source,), 256)
    record = CompactionRecordV1.model_validate_json(engine.result_store.read_reference(failure.value.compaction_record))
    assert len(record.calls) == 2 and all(call.error for call in record.calls)
    engine.reset_budget()
    prepared = await engine.assembler.prepare(
        system_message=engine.system_message,
        conversation=ConversationState(items=(source, UserMessage(run_id="active", content="Continue."))),
        active_run_id="active", policy=engine.policy, enable_new_strategy=True,
        result_store=engine.result_store, history_compactor=engine)
    assert prepared.usage.compaction_reason.startswith("CompactionError:")


@pytest.mark.asyncio
@pytest.mark.parametrize("inherited_id,first_id,expected_aliases", [
    ("msg_old", "msg_first", ["S0", "S1", "S2"]),
    ("S0", "S1", ["S2", "S3", "S4"]),
])
async def test_one_episode_reuses_aliases_across_prefix_chunks_and_reducer(
    tmp_path, inherited_id, first_id, expected_aliases,
):
    requests = []

    async def chat(request):
        requests.append(request)
        if request.items[-1].source_id == "compaction_control":
            return ModelResponse(content="garbled")
        return summary_response(request)

    items = (UserMessage(id=first_id, content="First"), UserMessage(id="msg_second", content="Second"))
    previous = ContextSummary(content=f"### Completed Work\n- Earlier. [{inherited_id}]", source_item_count=1)
    engine = compactor(tmp_path, chat, strategy="cache_aware")
    engine.source_request = ModelRequest(instructions="Original system.", items=(
        ModelUserInput(source_id="context_summary", content=previous.content),
        *(ModelUserInput(source_id=item.id, content=item.content) for item in items)))
    fits = engine._fits

    async def one_source_per_chunk(request):
        if request.items[0].source_id == "compaction_sources" and len(request_aliases(request.items[0].content)) > 1:
            return False
        return await fits(request)

    engine._fits = one_source_per_chunk
    summary = await engine.compact(previous, items, 256)
    assert request_aliases(requests[0].items[-1].content) == expected_aliases
    assert f"1 checkpoint {expected_aliases[0]}\n" in requests[0].items[-1].content
    for request in requests:
        rules = request.items[-1].content if request.items[-1].source_id == "compaction_control" else request.instructions
        assert f"cite {expected_aliases[0]}, never IDs shown inside it" in rules
    chunks = [request for request in requests if request.items[0].source_id == "compaction_sources"]
    reducers = [request for request in requests if request.items[0].source_id == "compaction_chunks"]
    assert [request_aliases(request.items[0].content) for request in chunks] == [[alias] for alias in expected_aliases]
    assert len(reducers) == 1
    assert request_aliases(reducers[0].items[0].content) == expected_aliases
    assert "msg_" not in reducers[0].items[0].content
    record = record_for(engine, summary)
    for task_summary in (record.task_summary, *(chunk.summary for chunk in record.chunks)):
        assert all(source_id in {"previous_summary", first_id, "msg_second"} for name in TaskSummary.model_fields
                   for entry in getattr(task_summary, name) for source_id in entry.source_ids)
    assert record.task_summary.requirements[0].source_ids == ("previous_summary",)
    assert record.task_summary.next_step[0].source_ids == ("msg_second",)


@pytest.mark.asyncio
async def test_warm_prefix_keeps_previous_checkpoint_bytes_and_lists_alias_positions(tmp_path):
    requests = []

    async def chat(request):
        requests.append(request)
        return summary_response(request)

    previous = ContextSummary(content="### Completed Work\n- Path C:\\data\\x stays exact. [msg_old]\n", source_item_count=1)
    checkpoint_turn = ModelUserInput(source_id="context_summary", content=previous.content)
    background = ModelUserInput(source_id="task", content="Current task.")
    items = (AssistantMessage(id="msg_call", tool_calls=(ToolCallItem(id="call", name="query"),)),
             ToolResultMessage(id="msg_result", call_id="call", name="query", status="completed", content=inline_content("Evidence")))
    prefix = (checkpoint_turn, background,
              ModelAssistantTurn(source_id="msg_call", tool_calls=(ToolCall(id="call", name="query"),)),
              ModelToolResultInput(source_id="msg_result", call_id="call", name="query", content="Evidence"))
    engine = compactor(tmp_path, chat, strategy="cache_aware")
    original = ModelRequest(instructions="Original system.", items=(*prefix, ModelUserInput(source_id="latest", content="Continue.")))
    engine.source_request = original
    await engine.compact(previous, items, 256)
    request = requests[0]
    assert len(requests) == 1
    assert request.items[:-1] == prefix
    assert request.items[0] is original.items[0]
    assert request.items[0].content.encode() == previous.content.encode()
    assert "Sources to summarize: S0, S1, S2\nHistory messages in order:\n1 checkpoint S0\n2 user background\n3 assistant S1\n4 tool S2\n" in request.items[-1].content


def test_markdown_alias_output_uses_fewer_tokens_than_json_real_ids():
    from dagent.harness_runtime.context import HeuristicTokenCounter

    source_ids = ["msg_" + f"{index:032x}" for index in range(2)]
    summary = TaskSummary(completed_work=tuple(
        SummaryEntry(text=f"Read config_{index}.yaml; timeout=30s", source_ids=tuple(source_ids)) for index in range(10)))
    aliases = compaction._SourceAliases(dict.fromkeys(source_ids, "Evidence"), None)
    counter = HeuristicTokenCounter()
    assert counter.count_text(compaction._render_with_aliases(summary, aliases)) < counter.count_text(
        json.dumps(summary.model_dump(mode="json", exclude_defaults=True)))


@pytest.mark.asyncio
@pytest.mark.parametrize("first_error", ["malformed", "oversized"])
async def test_format_and_size_errors_share_one_content_retry(tmp_path, first_error):
    requests = []

    async def chat(request):
        requests.append(request)
        oversized = (len(requests) == 1) == (first_error == "oversized")
        return ModelResponse(content=checkpoint(next_step=[("x" * 4000, ["S1"])]) if oversized else "garbled")

    engine = compactor(tmp_path, chat)
    with pytest.raises(CompactionError):
        await engine.compact(None, (UserMessage(id="source", content="Requirement."),), 256)
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_by", ["logical_calls", "http_attempts", "input_budget"])
async def test_malformed_retry_respects_remaining_budgets(tmp_path, blocked_by):
    requests = []

    async def chat(request):
        requests.append(request)
        return ModelResponse(content="garbled")

    engine = compactor(tmp_path, chat)
    if blocked_by == "input_budget":
        fits = engine._fits

        async def no_room_for_note(request):
            return "Your previous output" not in request.instructions and await fits(request)

        engine._fits = no_room_for_note
    else:
        setattr(engine, blocked_by, 7 if blocked_by == "logical_calls" else 11)
    with pytest.raises(CompactionError, match="invalid task summary"):
        await engine.compact(None, (UserMessage(id="source", content="Requirement."),), 256)
    assert len(requests) == 1
    assert engine.logical_calls <= 8 and engine.http_attempts <= 12


@pytest.mark.asyncio
async def test_skipped_checkpoint_content_logs_only_counts(tmp_path, caplog):
    async def chat(_request):
        return ModelResponse(content=("PRIVATE preamble\n### Unknown\n- PRIVATE outside [S1]\n"
                                      "### Next Step\n- PRIVATE uncited\n- Continue. [S1]"))

    engine = compactor(tmp_path, chat)
    summary = await engine.compact(None, (UserMessage(id="source", content="Requirement."),), 256)
    assert record_for(engine, summary).task_summary.next_step[0].text == "Continue."
    assert "skipped 1 unknown headings, 1 lines outside sections, 1 uncited entries and 1 other lines" in caplog.text
    assert "PRIVATE" not in caplog.text
