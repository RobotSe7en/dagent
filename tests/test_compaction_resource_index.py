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
async def test_incremental_archives_reuse_reads_across_prepare_operations(tmp_path):
    assembler = ContextAssembler(context_window_tokens=8192)
    meter = ReadMeter()
    summary = None
    steps = 40
    for index in range(steps):
        # Production creates both objects again for each context preparation.
        compactor = engine(tmp_path, assembler, meter)
        before = meter.bytes
        summary = await compactor.compact(
            summary, (UserMessage(id=f"source_{index:02d}", content="next requirement"),), 256,
        )
    last_read = meter.bytes - before
    disk_bytes = sum(path.stat().st_size for path in tmp_path.rglob("*.txt"))
    assert meter.calls < steps * 10
    assert last_read < disk_bytes // 4

    # A new runtime must verify existing history instead of trusting old cache.
    before = meter.bytes
    cold = engine(tmp_path, ContextAssembler(context_window_tokens=8192), meter)
    await cold.compact(summary, (UserMessage(id="after_restart", content="Continue."),), 256)
    assert meter.bytes - before > last_read * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replace", "delete"])
async def test_cached_archive_still_detects_changed_old_leaf(tmp_path, change):
    assembler = ContextAssembler(context_window_tokens=8192)
    meter = ReadMeter()
    first = engine(tmp_path, assembler, meter)
    summary = await first.compact(None, (UserMessage(id="old", content="Keep this evidence."),), 256)
    old_source = record_for(first, summary).source
    # Warm the closure index, then change a deep leaf behind the current root.
    second = engine(tmp_path, assembler, meter)
    summary = await second.compact(summary, (UserMessage(id="new", content="More evidence."),), 256)
    source_path = tmp_path / old_source.path
    if change == "delete":
        source_path.unlink()
    else:
        original = source_path.read_bytes()
        replacement = source_path.with_suffix(".replacement")
        replacement.write_bytes(b"x" * len(original))
        replacement.replace(source_path)
    with pytest.raises(CompactionArchiveError):
        await engine(tmp_path, assembler, meter).compact(
            summary, (UserMessage(id="latest", content="Continue."),), 256,
        )


@pytest.mark.asyncio
async def test_archive_index_does_not_cross_workspaces(tmp_path):
    assembler = ContextAssembler(context_window_tokens=8192)
    meter = ReadMeter()
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    compactor = engine(source, assembler, meter)
    summary = await compactor.compact(None, (UserMessage(content="Evidence."),), 256)
    summary = await engine(source, assembler, meter).compact(
        summary, (UserMessage(content="More evidence."),), 256,
    )
    with pytest.raises(CompactionArchiveError):
        await engine(destination, assembler, meter).compact(
            summary, (UserMessage(content="Continue."),), 256,
        )


@pytest.mark.asyncio
async def test_reference_continuation_reuses_archive_reads_but_rechecks_access(tmp_path):
    assembler = ContextAssembler(context_window_tokens=8192)
    meter = ReadMeter()
    summary = None
    first_source = None
    for index in range(30):
        compactor = engine(tmp_path, assembler, meter)
        compactor.policy = ContextPolicy(compaction_strategy="chunked", allow_reference_fallback=True)
        compactor.read_available = True
        summary = await compactor.reference_fallback(
            summary, (UserMessage(id=f"source_{index}", content="Retained evidence."),),
            256, reason="Summary service unavailable.",
        )
        if first_source is None:
            first_source = record_for(compactor, summary).source
    assert meter.calls < 30 * 10

    # Permission is checked even when the old node's parsed links are cached.
    compactor = engine(tmp_path, assembler, meter)
    compactor.policy = ContextPolicy(compaction_strategy="chunked", allow_reference_fallback=True)
    compactor.read_available = lambda ref: ref.path != first_source.path
    with pytest.raises(CompactionArchiveError, match="available read capability"):
        await compactor.reference_fallback(
            summary, (UserMessage(content="Continue."),), 256, reason="offline",
        )
