"""Compaction writes its archives without re-reading earlier history first."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dagent.harness_runtime.compaction import CompactionArchiveError, HistoryCompactor
from dagent.harness_runtime.context import ContextAssembler
from dagent.harness_runtime.result_storage import ResultStore
from dagent.schemas.context import ContextPolicy
from dagent.schemas.conversation import UserMessage
from tests.test_compaction_strategy import record_for, summary_response


class ReadMeter:
    def __init__(self) -> None:
        self.calls = 0
        self.bytes = 0


class MeteredStore(ResultStore):
    def __init__(self, workspace: Path, meter: ReadMeter) -> None:
        super().__init__(workspace, ".runtime")
        self.meter = meter

    def read_reference(self, reference):
        self.meter.calls += 1
        self.meter.bytes += reference.byte_length
        return super().read_reference(reference)


def engine(workspace, assembler, meter):
    async def summarize(request):
        return summary_response(request)

    return HistoryCompactor(
        policy=ContextPolicy(compaction_strategy="chunked"),
        assembler=assembler, result_store=MeteredStore(workspace, meter),
        system_message={"content": "Keep the task state."}, chat=summarize,
    )


@pytest.mark.asyncio
async def test_each_compaction_reads_a_bounded_number_of_archives(tmp_path):
    meter = ReadMeter()
    summary = None
    reads = []
    for index in range(40):
        before = meter.calls
        summary = await engine(tmp_path, ContextAssembler(context_window_tokens=8192), meter).compact(
            summary, (UserMessage(id=f"source_{index:02d}", content="next requirement"),), 256,
        )
        reads.append(meter.calls - before)
    # Only the latest record and the new source are read, however long the history.
    assert max(reads) <= 3
    assert set(reads[1:]) == {reads[1]}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace", "delete"])
async def test_changed_old_archive_does_not_block_a_new_compaction(tmp_path, change):
    assembler = ContextAssembler(context_window_tokens=8192)
    meter = ReadMeter()
    first = engine(tmp_path, assembler, meter)
    summary = await first.compact(None, (UserMessage(id="old", content="Keep this evidence."),), 256)
    old_source = record_for(first, summary).source
    summary = await engine(tmp_path, assembler, meter).compact(
        summary, (UserMessage(id="new", content="More evidence."),), 256,
    )
    source_path = tmp_path / old_source.path
    if change == "delete":
        source_path.unlink()
    else:
        source_path.write_bytes(b"x" * source_path.stat().st_size)
    latest = await engine(tmp_path, assembler, meter).compact(
        summary, (UserMessage(id="latest", content="Continue."),), 256,
    )
    assert latest.compaction_record is not None


@pytest.mark.asyncio
async def test_reference_fallback_requires_read_file_and_reads_no_history(tmp_path):
    assembler = ContextAssembler(context_window_tokens=8192)
    meter = ReadMeter()
    summary = None
    for index in range(30):
        compactor = engine(tmp_path, assembler, meter)
        compactor.policy = ContextPolicy(compaction_strategy="chunked", allow_reference_fallback=True)
        compactor.read_available = True
        before = meter.calls
        summary = await compactor.reference_fallback(
            summary, (UserMessage(id=f"source_{index}", content="Retained evidence."),),
            256, reason="Summary service unavailable.",
        )
        assert meter.calls - before <= 2
        assert summary.method == "reference_fallback"

    compactor = engine(tmp_path, assembler, meter)
    compactor.policy = ContextPolicy(compaction_strategy="chunked", allow_reference_fallback=True)
    compactor.read_available = False
    with pytest.raises(CompactionArchiveError, match="read_file"):
        await compactor.reference_fallback(
            summary, (UserMessage(content="Continue."),), 256, reason="offline",
        )


@pytest.mark.asyncio
async def test_missing_earlier_record_degrades_compaction_without_failing(tmp_path):
    assembler = ContextAssembler(context_window_tokens=8192)
    meter = ReadMeter()
    workspace = tmp_path / "run"
    first = await engine(workspace, assembler, meter).compact(None, (UserMessage(id="a", content="first"),), 256)
    # Lost from the workspace (deleted, or a continuation elsewhere).
    (workspace / first.compaction_record.path).unlink()
    compactor = engine(workspace, assembler, meter)
    second = await compactor.compact(first, (UserMessage(id="b", content="second"),), 256)
    record = record_for(compactor, second)
    # The link is kept as written; the lost history is marked unresolved.
    assert record.previous_record == first.compaction_record and record.prior_coverage == "unknown"
    assert record.has_unresolved_history and "[DEGRADED_CONTEXT]" in second.content


@pytest.mark.asyncio
async def test_archive_chain_is_read_regardless_of_the_model_read_boundary(tmp_path):
    from dagent.schemas.common import Boundary

    async def summarize(request):
        return summary_response(request)

    def bounded():
        return HistoryCompactor(
            policy=ContextPolicy(compaction_strategy="chunked", allow_reference_fallback=True),
            assembler=ContextAssembler(context_window_tokens=8192),
            result_store=ResultStore(tmp_path, ".runtime", read_boundary=Boundary(allowed_paths=["src"])),
            system_message={"content": "Keep the task state."}, chat=summarize, read_available=True,
        )

    first = await bounded().compact(None, (UserMessage(id="a", content="first"),), 256)
    compactor = bounded()
    second = await compactor.compact(first, (UserMessage(id="b", content="second"),), 256)
    # The runtime reads its own archives; only the model is bounded.
    record = record_for(compactor, second)
    assert record.previous_record == first.compaction_record and not record.has_unresolved_history
    # The model could not open the source archive, so no reference fallback.
    assert not compactor.read_available
    with pytest.raises(CompactionArchiveError, match="read boundary"):
        await compactor.reference_fallback(second, (UserMessage(id="c", content="third"),), 256, reason="offline")


@pytest.mark.asyncio
async def test_sources_of_failed_records_that_cannot_be_read_are_summarized_again(tmp_path):
    import dagent
    from dagent.providers.model_io import ModelTokenCount
    from dagent.providers.openai_compatible import ProviderTokenCountError

    paid = round_paid = 0

    async def count(request, _field):
        if request.purpose == "compaction":
            return ModelTokenCount(count=100, max_model_len=8192, exact=True, source="render")
        remaining = len({item.source_id for item in request.items} & {"A", "B"})
        if remaining == 0 and round_paid == 2:
            raise ProviderTokenCountError("Counting failed after the paid summaries.")
        return ModelTokenCount(count={2: 11000, 1: 9000, 0: 100}[remaining], max_model_len=8192,
                               exact=True, source="render")

    async def summarize(request):
        nonlocal paid, round_paid
        paid += 1
        round_paid += 1
        response = summary_response(request)
        # A later summary differs, so its record gets a new path.
        return replace(response, content=response.content.replace("pending", f"pending {paid}"))

    conversation = dagent.ConversationState(items=(
        dagent.UserMessage(id="first_task", run_id="run", content="First task."),
        dagent.AssistantMessage(id="A", run_id="run", content="First evidence."),
        dagent.UserMessage(id="steer", run_id="run", content="Keep this correction."),
        dagent.AssistantMessage(id="B", run_id="run", content="Second evidence."),
        dagent.AssistantMessage(id="tail", run_id="run", content="Latest protected step."),
    ))
    policy = dagent.ContextPolicy(compaction_strategy="chunked", compaction_retain_ratio=.0001)
    assembler = ContextAssembler(context_window_tokens=8192, max_output_tokens=512, request_token_counter=count)
    store = ResultStore(tmp_path / "run", ".runtime")

    async def failed_state(source):
        nonlocal round_paid
        round_paid = 0
        with pytest.raises(ProviderTokenCountError) as failed:
            await assembler.prepare(
                system_message={"content": "Task"}, conversation=source, policy=policy,
                result_store=store, enable_new_strategy=True,
                history_compactor=HistoryCompactor(policy=policy, assembler=assembler,
                    result_store=store, system_message={"content": "Task"}, chat=summarize),
            )
        return failed.value._conversation

    saved = await failed_state(conversation)
    missing = {failure.record.path for failure in saved.compaction_state.failed_records}
    assert paid == 2 and len(missing) == 2
    for path in missing:
        (tmp_path / "run" / path).unlink()
    # The lost replacements cannot be reused, so their sources are summarized
    # again instead of staying blocked.
    again = await failed_state(saved)
    assert paid == 4
    fresh = {failure.record.path for failure in again.compaction_state.failed_records} - missing
    assert len(fresh) == 2 and all((tmp_path / "run" / path).is_file() for path in fresh)


@pytest.mark.asyncio
async def test_missing_legacy_result_manifest_leaves_its_results_out(tmp_path):
    import json

    from dagent.schemas import ContextSummary, ConversationState
    from dagent.schemas.conversation import AssistantMessage, ToolCallItem, ToolResultMessage, inline_content

    store = ResultStore(tmp_path, ".runtime")

    def call(call_id):
        return (AssistantMessage(tool_calls=(ToolCallItem(id=call_id, name="tool_data"),)),
                ToolResultMessage(call_id=call_id, name="tool_data", status="completed",
                                  content=inline_content(f"{call_id} result")))

    earlier, lost = store.archive(None, call("old"))
    assert not lost
    (tmp_path / earlier.path).unlink()
    conversation = ConversationState(
        summary=ContextSummary(content="Earlier work.", source_item_count=2, result_manifest=earlier),
        items=(*call("new"), UserMessage(content="Continue.")),
    )

    async def summarize(previous, items, limit):
        return ContextSummary(content="Summary.", source_item_count=previous.source_item_count + len(items))

    compacted, method, _ = await ContextAssembler()._compact_slice(
        conversation, start=0, end=2, policy=ContextPolicy(), compact=summarize, result_store=store,
    )
    summary = compacted.summary
    assert method == "model" and summary.result_archive_incomplete
    lines = [json.loads(line) for line in (tmp_path / summary.result_manifest.path).read_text().splitlines()]
    assert [line.get("call_id") for line in lines if line.get("type") == "tool_result"] == ["new"]
