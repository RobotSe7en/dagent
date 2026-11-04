"""Compaction writes its archives without re-reading earlier history first."""

from __future__ import annotations

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
