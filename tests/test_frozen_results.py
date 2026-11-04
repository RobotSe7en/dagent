"""Frozen displays keep the first budget decision; overflow files are best-effort."""

import hashlib
import json
import shutil

import pytest

from dagent.harness_runtime.context import HeuristicTokenCounter
from dagent.harness_runtime.conversation_resources import ConversationResourceError, ConversationResourceStore
from dagent.harness_runtime.frozen_results import (
    freeze_result_displays,
    reference_frozen_result_displays,
)
from dagent.harness_runtime.result_projection import ResultBudgetExceeded, project_result, read_file_available
from dagent.harness_runtime.result_storage import ResultStore
from dagent.schemas.context import ContextPolicy
from dagent.schemas.conversation import (
    AssistantMessage, ContentReference, ContextSummary, ConversationState, InlineContent,
    FrozenResultDisplay, ToolCallItem, ToolResultMessage,
)
from dagent.schemas.retention import ResultRetention, ResultCursor


def _conversation(*texts):
    return ConversationState(items=(
        AssistantMessage(tool_calls=tuple(ToolCallItem(id=str(i), name="tool_data") for i in range(len(texts)))),
        *(ToolResultMessage(call_id=str(i), name="tool_data", status="completed", content=InlineContent(text=text))
          for i, text in enumerate(texts)),
    ))


def _freeze(conversation, store, budget=2**60, *, files=True, cap=128):
    return freeze_result_displays(
        conversation, result_store=store, policy=ContextPolicy(max_tool_result_tokens=cap),
        counter=HeuristicTokenCounter(), files=files, total_budget_tokens=budget,
    )


def _emergency(conversation, store, ids, *, files=True):
    return reference_frozen_result_displays(conversation, result_store=store, files=files, result_ids=ids)


def _reader_tools():
    return [{"type": "function", "function": {
        "name": "tool_read_file", "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "offset_chars": {"type": "integer"},
            "limit_chars": {"type": "integer"},
        }},
    }}]


def _archive_chain(store, items=()):
    """A compaction source and record, as compaction writes them."""
    from dagent.harness_runtime.compaction import CompactionRecordV1, CompactionSourceV1

    source = store.snapshot_text(CompactionSourceV1(items=items).model_dump_json())
    record = store.snapshot_text(CompactionRecordV1(
        status="complete", source=source, source_digest=source.sha256, source_ranges=(), chunks=(),
    ).model_dump_json())
    return source, record


def test_new_batch_shares_budget_and_retains_unicode_source_spans(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    sources = ("开头🙂" * 800 + "结尾", "second " * 1500)
    frozen = _freeze(_conversation(*sources), store, budget=600, cap=1024)
    results = frozen.items[1:]
    counter = HeuristicTokenCounter()
    assert sum(counter.count_text(result.model_display.text) for result in results) <= 600
    for result, original in zip(results, sources):
        display = result.model_display
        assert display.revision == 0
        assert display.displayed_spans
        assert display.recovery.sha256 is None
        assert store.read_reference(display.recovery).decode() == original
        assert result.content == display.recovery
        for start, end in display.displayed_spans:
            assert original[start:end] in display.text
        assert "offset_chars" in display.text
        assert "TRUNCATED" in display.text
    assert frozen.items[0] == _conversation(*sources).items[0].model_copy(update={"id": frozen.items[0].id})


def test_complete_display_writes_no_file(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    original = "small result " * 10
    frozen = _freeze(_conversation(original), store, cap=1024)
    result = frozen.items[1]
    assert result.model_display.recovery is None
    assert result.content == InlineContent(text=original)
    assert original in result.model_display.text
    assert "Recover received result" not in result.model_display.text
    assert "TRUNCATED" not in result.model_display.text
    assert not (tmp_path / ".runtime").exists()


def test_existing_display_does_not_change_with_policy_or_budget(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    frozen = _freeze(_conversation("data " * 2000), store)
    result = frozen.items[1]
    unchanged = _freeze(frozen, store, budget=0, cap=2048)
    assert unchanged is frozen
    projected = project_result(result, 1, HeuristicTokenCounter(), read_available=True)
    assert projected.text == result.model_display.text
    assert projected.displayed_spans == result.model_display.displayed_spans


def test_metadata_floor_can_exceed_per_result_body_cap(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    frozen = _freeze(_conversation("中文" * 1000), store, cap=64)
    display = frozen.items[1].model_display
    assert HeuristicTokenCounter().count_text(display.text) > 64
    assert display.displayed_spans
    with pytest.raises(ResultBudgetExceeded, match="Minimum information") as raised:
        _freeze(_conversation("data"), store, budget=1, cap=64)
    minimum = raised.value.minimum_tokens
    assert minimum is not None and minimum > 1
    at_floor = _freeze(_conversation("data"), store, budget=minimum, cap=64)
    assert HeuristicTokenCounter().count_text(at_floor.items[1].model_display.text) == minimum


def test_shortening_without_read_file_writes_no_file(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    original = "important data " * 1000
    frozen = _freeze(_conversation(original), store, files=False, cap=64)
    result = frozen.items[1]
    display = result.model_display
    assert "[TRUNCATED]" in display.text and "[RECOVERY_UNAVAILABLE: no saved original]" in display.text
    assert original not in display.text and "path=" not in display.text
    assert display.recovery is None
    assert sum(end - start for start, end in display.displayed_spans) < len(original)
    assert result.content == InlineContent(text=original)
    assert not (tmp_path / ".runtime").exists()
    projection = project_result(result, 64, HeuristicTokenCounter(), read_available=False)
    assert projection.truncated and projection.unrecoverable
    # A small result is still shown completely and marked as nothing.
    small = _freeze(_conversation("short"), store, files=False, cap=64).items[1].model_display
    assert "[TRUNCATED]" not in small.text and "RECOVERY_UNAVAILABLE" not in small.text
    assert _emergency(frozen, store, {result.id}, files=False) is frozen


def test_nul_text_is_shortened_without_a_file_read_file_cannot_open(tmp_path):
    from dagent.capabilities.tools.file_tools import read_file

    store = ResultStore(tmp_path, ".runtime")
    source = "a" * 5000 + "\0" + "b" * 5000
    # Why: read_file rejects a file containing NUL.
    probe = store.save_text("probe", source)
    with pytest.raises(ValueError, match="not a UTF-8 text file"):
        read_file(tmp_path / probe.path, offset_chars=4900, limit_chars=200)
    (tmp_path / probe.path).unlink()
    frozen = _freeze(_conversation(source), store, cap=64)
    result = frozen.items[1]
    display = result.model_display
    # Shortened without a file, like a model that cannot open files.
    assert "[TRUNCATED]" in display.text and "[RECOVERY_UNAVAILABLE: no saved original]" in display.text
    assert source not in display.text and "path=" not in display.text
    assert sum(end - start for start, end in display.displayed_spans) < len(source)
    assert display.recovery is None and result.content == InlineContent(text=source)
    assert _emergency(frozen, store, {result.id}) is frozen
    assert not any((tmp_path / ".runtime" / "results").iterdir())


def test_read_file_tool_is_detected_by_its_exact_name():
    assert read_file_available(_reader_tools())
    assert read_file_available([{"type": "function", "function": {"name": "tool_read_file"}}])
    assert not read_file_available([{"type": "function", "function": {"name": "tool_shell"}}])
    assert not read_file_available([{"type": "function", "function": {"name": "tool_not_read_file"}}])
    assert not read_file_available([])


def test_saved_overflow_is_shown_from_its_preview_without_opening_the_file(tmp_path, monkeypatch):
    store = ResultStore(tmp_path, ".runtime")
    original = "received " * 40000
    saved = store.save_text("call", original)
    result = _conversation("").items[1].model_copy(update={"content": saved})
    conversation = _conversation("").model_copy(update={"items": (_conversation("").items[0], result)})
    (tmp_path / saved.path).unlink()

    def unexpected_read(self, reference):
        raise AssertionError("A saved overflow file must not be opened.")

    monkeypatch.setattr(ResultStore, "read_reference", unexpected_read)
    for files in (True, False):
        frozen = _freeze(conversation, store, files=files, cap=1024)
        display = frozen.items[1].model_display
        assert display.recovery == saved
        assert frozen.items[1].content == saved
        assert "[TRUNCATED]" in display.text
        assert f"path={saved.path}" in display.text
        emergency = _emergency(frozen, store, {frozen.items[1].id}, files=files)
        if not files:
            # The transition removes displayed text, so it needs read access.
            assert emergency is frozen
            continue
        changed = emergency.items[1].model_display
        assert changed.revision == 1 and changed.recovery == saved and changed.previous_display is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tools", [_reader_tools(), []])
@pytest.mark.parametrize("complete", [False, True])
async def test_old_frozen_receipt_is_kept_even_when_its_file_is_missing(tmp_path, tools, complete):
    from dagent.harness_runtime.context import ContextAssembler

    store = ResultStore(tmp_path, ".runtime")
    source = "Unicode文字🙂" * 1000
    conversation = _conversation(source)
    reference = ContentReference(
        path=".runtime/history/" + "0" * 64 + ".txt", media_type="text/plain; charset=utf-8",
        byte_length=len(source.encode()), sha256="0" * 64,
    )
    body = source if complete else source[:10] + "\n…\n" + source[-10:]
    # A receipt saved by an earlier SDK version, whose snapshot is gone.
    display = FrozenResultDisplay(
        text="[status=completed]\n[TRUNCATED] historical metadata\n" + body, recovery=reference,
    )
    result = conversation.items[1].model_copy(update={"content": reference, "model_display": display})
    conversation = conversation.model_copy(update={"items": (conversation.items[0], result)})
    assembler = ContextAssembler(context_window_tokens=131072, max_output_tokens=8192)
    options = dict(system_message={"content": "instructions"}, conversation=conversation,
                   result_store=store, tools=tools, enable_new_strategy=True)
    prepared = await assembler.prepare(policy=ContextPolicy(compaction_strategy="cache_aware"), **options)
    assert prepared.conversation.items[1].model_display == display
    assert prepared.request.items[-1].content == display.text
    legacy = await assembler.prepare(policy=ContextPolicy(), **options)
    assert legacy.conversation.items[1].model_display == display
    assert legacy.request.items[-1].content == display.text


@pytest.mark.asyncio
async def test_empty_frozen_body_needs_no_read_file(tmp_path):
    from dagent.harness_runtime.context import ContextAssembler

    store = ResultStore(tmp_path, ".runtime")
    frozen = _freeze(_conversation(""), store, files=False)
    prepared = await ContextAssembler().prepare(
        system_message={"content": "instructions"}, conversation=frozen,
        result_store=store, policy=ContextPolicy(compaction_strategy="cache_aware"), enable_new_strategy=True,
    )
    assert prepared.conversation.items[1].model_display == frozen.items[1].model_display


def test_reviewer_feedback_is_mandatory_and_separate_from_received_body(tmp_path):
    from dagent.harness_runtime.compaction import _source_texts

    store = ResultStore(tmp_path, ".runtime")
    source = "received evidence " * 1000
    feedback = "Do not repeat the operation. " * 50
    conversation = _conversation(source)
    item = conversation.items[1].model_copy(update={"reviewer_feedback": feedback})
    conversation = conversation.model_copy(update={"items": (conversation.items[0], item)})
    frozen = _freeze(conversation, store, cap=64)
    result = frozen.items[1]
    assert f"Reviewer feedback: {feedback}" in result.model_display.text
    assert store.read_reference(result.model_display.recovery).decode() == source
    for start, end in result.model_display.displayed_spans:
        assert source[start:end] in result.model_display.text
    assert feedback in _source_texts(None, (result,))[result.id]
    assert feedback in _source_texts(None, (item,))[item.id]
    emergency = _emergency(frozen, store, {result.id})
    assert f"Reviewer feedback: {feedback}" in emergency.items[1].model_display.text
    assert emergency.items[1].model_display.displayed_spans == ()
    with pytest.raises(ResultBudgetExceeded, match="Minimum information"):
        _freeze(conversation, store, budget=64, cap=64)


def test_file_window_coordinates_remain_distinct_from_snapshot_spans(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    source = "中文🙂" * 1000
    conversation = _conversation(source)
    result = conversation.items[1].model_copy(update={"retention": ResultRetention(
        source_completeness="partial", window_start=250, window_length=len(source),
        continuation=ResultCursor(tool="read_file", path="original.txt", offset=250 + len(source)),
    )})
    frozen = _freeze(conversation.model_copy(update={"items": (conversation.items[0], result)}), store)
    display = frozen.items[1].model_display
    assert display.displayed_spans[0][0] == 0
    assert "File window: chars=[250," in display.text
    assert frozen.items[1].retention == result.retention


@pytest.mark.parametrize("completeness", ["partial", "unknown", "complete"])
def test_received_window_recovers_from_overflow_file_while_source_cursor_skips_received_text(tmp_path, completeness):
    store = ResultStore(tmp_path, ".runtime")
    received = "received🙂证据" * 600
    source_path = tmp_path / "original.txt"
    source_path.write_text("p" * 250 + received + "unreceived tail", encoding="utf-8")
    conversation = _conversation(received)
    source_end = 250 + len(received)
    result = conversation.items[1].model_copy(update={"retention": ResultRetention(
        source_completeness=completeness, window_start=250, window_length=len(received),
        continuation=ResultCursor(tool="read_file", path="original.txt", offset=source_end, limit=37),
    )})
    frozen = _freeze(conversation.model_copy(update={"items": (conversation.items[0], result)}), store)
    display = frozen.items[1].model_display
    shown_end = display.displayed_spans[0][1]
    assert 0 < shown_end < len(received)

    # The original can change after the result was received; its continuation
    # must never be used to reconstruct undisplayed received evidence.
    source_path.write_text("n" * source_end + "new source tail", encoding="utf-8")
    recover_prefix = "[Recover received result with read_file: "
    recover_line = next(line for line in display.text.splitlines() if line.startswith(recover_prefix))
    recovery = json.loads(recover_line[len(recover_prefix):-1])
    assert recovery["path"] == display.recovery.path
    assert recovery["offset_chars"] == shown_end
    snapshot = store.read_reference(display.recovery).decode("utf-8")
    assert snapshot[recovery["offset_chars"]:][:recovery["limit_chars"]] == received[shown_end:][:1024]
    assert snapshot == received

    source_prefix = "[Continue source with read_file: "
    source_lines = [line for line in display.text.splitlines() if line.startswith(source_prefix)]
    assert "[Continue with read_file:" not in display.text
    if completeness == "complete":
        assert source_lines == []
    else:
        continuation = json.loads(source_lines[0][len(source_prefix):-1])
        assert continuation == {"path": "original.txt", "offset_chars": source_end, "limit_chars": 37}
        assert source_path.read_text()[continuation["offset_chars"]:] == "new source tail"


def test_legacy_file_window_cursor_still_continues_from_display_end():
    conversation = _conversation("received evidence " * 100)
    result = conversation.items[1].model_copy(update={"retention": ResultRetention(
        source_completeness="complete", window_start=250, window_length=1800,
        continuation=ResultCursor(tool="read_file", path="original.txt", offset=2050, limit=37),
    )})
    projection = project_result(result, 4096, HeuristicTokenCounter(), read_available=True, max_body_chars=17)
    assert '[Continue with read_file: {"path": "original.txt", "offset_chars": 267, "limit_chars": 17}]' in projection.text
    assert "Continue source" not in projection.text and "Recover received" not in projection.text


def test_emergency_reference_is_one_way_and_reads_no_file(tmp_path, monkeypatch):
    store = ResultStore(tmp_path, ".runtime")
    frozen = _freeze(_conversation("original " * 1000), store)
    result = frozen.items[1]

    def unexpected_read(self, reference):
        raise AssertionError("The emergency transition must not open saved files.")

    monkeypatch.setattr(ResultStore, "read_reference", unexpected_read)
    (tmp_path / result.model_display.recovery.path).unlink()
    emergency = _emergency(frozen, store, {result.id})
    changed = emergency.items[1]
    assert changed.call_id == result.call_id and changed.status == result.status
    assert emergency.items[0] == frozen.items[0]
    assert changed.model_display.revision == 1
    assert changed.model_display.displayed_spans == ()
    assert changed.model_display.previous_display is None
    assert changed.model_display.recovery == result.model_display.recovery
    assert _emergency(emergency, store, {result.id}) is emergency


def test_emergency_reference_writes_a_file_for_a_complete_display_only_with_read_file(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    original = "complete " * 50
    frozen = _freeze(_conversation(original), store, cap=4096)
    assert frozen.items[1].model_display.recovery is None
    assert _emergency(frozen, store, {frozen.items[1].id}, files=False) is frozen
    emergency = _emergency(frozen, store, {frozen.items[1].id})
    changed = emergency.items[1]
    assert changed.model_display.revision == 1
    assert store.read_reference(changed.model_display.recovery).decode() == original
    assert changed.content == changed.model_display.recovery


def test_display_storage_failure_keeps_the_complete_result_inline(tmp_path, monkeypatch):
    store = ResultStore(tmp_path, ".runtime")
    original = "preserve all received evidence " * 400

    def fail(self, reference, text):
        raise OSError("disk full")

    monkeypatch.setattr(ResultStore, "write_text", fail)
    with pytest.warns(RuntimeWarning, match="disk full"):
        frozen = _freeze(_conversation(original), store, cap=64)
    result = frozen.items[1]
    assert result.content == InlineContent(text=original)
    assert result.model_display.recovery is None
    assert len(result.retention.storage_warnings) == 1
    assert "[RECOVERY_UNAVAILABLE: no saved original]" in result.model_display.text
    assert "storage warning:" in result.model_display.text


def test_saved_files_are_read_as_they_are_now(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    saved = store.save_text("call", "original")
    assert saved.sha256 is None
    (tmp_path / saved.path).write_text("changed later")
    assert store.read_reference(saved) == b"changed later"
    archive = store.snapshot_text("archive")
    assert archive.sha256 == hashlib.sha256(b"archive").hexdigest()
    (tmp_path / archive.path).write_text("changed!")
    assert store.snapshot_text("archive") == archive


def test_tool_output_is_neither_persisted_nor_restored_into_a_new_run(tmp_path, monkeypatch):
    import pathlib

    workspace = tmp_path / "first"
    result_store = ResultStore(workspace, ".runtime")
    frozen = _freeze(_conversation("source " * 1000, "other " * 1000), result_store)
    frozen = _emergency(frozen, result_store, {frozen.items[1].id})
    recovery = frozen.items[1].model_display.recovery
    reads = []
    original = pathlib.Path.read_bytes

    def counted(self):
        reads.append(self.name)
        return original(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", counted)
    resources = ConversationResourceStore(tmp_path, ".runtime")
    resources.persist(frozen, workspace_path=workspace)
    shutil.rmtree(workspace)
    destination = tmp_path / "next"
    assert resources.materialize(frozen, workspace_path=destination) == frozen
    assert reads == []
    assert not (destination / recovery.path).exists()
    assert not resources.root.exists()


def test_repeated_boundaries_reuse_verified_records_and_legacy_results(tmp_path, monkeypatch):
    import pathlib

    workspace = tmp_path / "first"
    store = ResultStore(workspace, ".runtime")
    frozen = _freeze(_conversation("first " * 1000), store)
    _, record = _archive_chain(store, frozen.items)
    legacy = ToolResultMessage(call_id="legacy", name="tool_legacy", status="completed",
                               content=store.save_text("legacy", "legacy result body"))
    conversation = ConversationState(id=frozen.id, items=(*frozen.items, legacy), summary=ContextSummary(
        content="summary", source_item_count=2, compaction_record=record))
    resources = ConversationResourceStore(tmp_path, ".runtime")
    resources.persist(conversation, workspace_path=workspace)
    destination = tmp_path / "next"
    restored = resources.materialize(conversation, workspace_path=destination)
    assert restored == conversation
    reads = []
    original = pathlib.Path.read_bytes

    def counted(self):
        reads.append(self.name)
        return original(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", counted)
    resources.persist(restored, workspace_path=destination)
    assert resources.materialize(restored, workspace_path=destination) == restored
    # Records and archived sources are unchanged; tool output is never read.
    assert reads == []


def test_persist_rewrites_a_deleted_archive_object(tmp_path):
    workspace = tmp_path / "first"
    store = ResultStore(workspace, ".runtime")
    _, record = _archive_chain(store)
    conversation = ConversationState(summary=ContextSummary(
        content="summary", source_item_count=1, compaction_record=record))
    resources = ConversationResourceStore(tmp_path, ".runtime")
    resources.persist(conversation, workspace_path=workspace)
    stored = resources._object_path(conversation.id, record.sha256)
    stored.unlink()
    resources.persist(conversation, workspace_path=workspace)
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == record.sha256


def test_verified_resource_caches_are_bounded(tmp_path, monkeypatch):
    from dagent.harness_runtime import conversation_resources
    from dagent.harness_runtime.compaction import CompactionRecordV1, CompactionSourceV1

    monkeypatch.setattr(conversation_resources, "_CACHE_LIMIT", 3)
    workspace = tmp_path / "first"
    store = ResultStore(workspace, ".runtime")
    previous = None
    for index in range(4):
        source = store.snapshot_text(CompactionSourceV1(items=(), previous_summary=str(index)).model_dump_json())
        previous = store.snapshot_text(CompactionRecordV1(
            status="complete", source=source, source_digest=source.sha256, source_ranges=(),
            chunks=(), previous_record=previous,
        ).model_dump_json())
    conversation = ConversationState(summary=ContextSummary(
        content="summary", source_item_count=1, compaction_record=previous))
    resources = ConversationResourceStore(tmp_path, ".runtime")
    resources.persist(conversation, workspace_path=workspace)
    resources.materialize(conversation, workspace_path=tmp_path / "next")
    assert len(resources._verified) <= 3 and len(resources._edges) <= 3


def test_result_json_is_not_interpreted_as_a_resource_manifest(tmp_path):
    store = ResultStore(tmp_path / "first", ".runtime")
    source = json.dumps({"type": "dagent_compaction_source", "refs": [{"path": "must-not-read"}]})
    frozen = _freeze(_conversation(source), store)
    resources = ConversationResourceStore(tmp_path, ".runtime")
    resources.persist(frozen, workspace_path=store.workspace)
    restored = resources.materialize(frozen, workspace_path=tmp_path / "next")
    assert restored.items[1].model_display.text == frozen.items[1].model_display.text


def test_snapshot_rejects_a_symlink_outside_the_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    store = ResultStore(workspace, ".runtime")
    outside = tmp_path / "outside"
    outside.mkdir()
    store.snapshot_root.parent.mkdir(parents=True)
    store.snapshot_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        store.snapshot_text("must stay in the workspace")
    assert list(outside.iterdir()) == []


def test_compaction_record_restores_its_archive_chain_but_not_tool_output(tmp_path):
    from dagent.harness_runtime.compaction import CompactionRecordV1, CompactionSourceV1

    workspace = tmp_path / "first"
    store = ResultStore(workspace, ".runtime")
    frozen = _freeze(_conversation("first " * 1000), store)
    result = frozen.items[1]
    # Older sources also listed tool output in ``refs``; it is no longer followed.
    archive = CompactionSourceV1(items=frozen.items, refs=(result.model_display.recovery,))
    source = store.snapshot_text(archive.model_dump_json())
    record = CompactionRecordV1(status="complete", source=source, source_digest=source.sha256,
                                source_ranges=(), chunks=())
    previous = store.snapshot_text(record.model_dump_json())
    next_source = store.snapshot_text(CompactionSourceV1(items=(), previous_summary="first summary").model_dump_json())
    latest = store.snapshot_text(CompactionRecordV1(
        status="reference_only", source=next_source, source_digest=next_source.sha256,
        source_ranges=(), chunks=(), previous_record=previous,
    ).model_dump_json())
    conversation = ConversationState(id=frozen.id, summary=ContextSummary(
        content="summary", source_item_count=2, compaction_record=latest,
    ))
    resources = ConversationResourceStore(tmp_path, ".runtime")
    resources.persist(conversation, workspace_path=workspace)
    shutil.rmtree(workspace)
    destination = tmp_path / "next"
    assert resources.materialize(conversation, workspace_path=destination) == conversation
    for reference in (latest, next_source, previous, source):
        data = (destination / reference.path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == reference.sha256
    assert not (destination / result.model_display.recovery.path).exists()
    # Archive verification itself is unchanged in this release.
    (destination / previous.path).write_text("broken")
    with pytest.raises(ConversationResourceError, match="size mismatch"):
        resources.materialize(conversation, workspace_path=destination)


def test_missing_archive_fails_a_new_run_clearly(tmp_path):
    workspace = tmp_path / "first"
    store = ResultStore(workspace, ".runtime")
    _, record = _archive_chain(store)
    conversation = ConversationState(summary=ContextSummary(
        content="summary", source_item_count=1, compaction_record=record))
    resources = ConversationResourceStore(tmp_path, ".runtime")
    shutil.rmtree(workspace)
    with pytest.raises(ConversationResourceError, match="cannot be read"):
        resources.materialize(conversation, workspace_path=tmp_path / "next")


def test_failed_record_and_reused_partial_record_restore_their_archive_chain(tmp_path):
    from dagent.harness_runtime.compaction import CompactionRecordV1, CompactionSourceV1
    from dagent.schemas.conversation import CompactionState, FailedCompaction

    workspace = tmp_path / "first"
    store = ResultStore(workspace, ".runtime")
    result = _freeze(_conversation("original evidence " * 100), store).items[1]
    source = store.snapshot_text(CompactionSourceV1(
        items=(result,), refs=(result.model_display.recovery,),
    ).model_dump_json())
    partial = store.snapshot_text(CompactionRecordV1(
        status="partial", source=source, source_digest=source.sha256,
        source_ranges=(), chunks=(),
    ).model_dump_json())
    next_source = store.snapshot_text(CompactionSourceV1(items=()).model_dump_json())
    reused = store.snapshot_text(CompactionRecordV1(
        status="reference_only", source=next_source, source_digest=next_source.sha256,
        source_ranges=(), chunks=(), reused_from=partial,
    ).model_dump_json())
    conversation = ConversationState(compaction_state=CompactionState(failed_records=(
        FailedCompaction(source_keys=("failed",), record=reused),
    )))
    resources = ConversationResourceStore(tmp_path, ".runtime")
    resources.persist(conversation, workspace_path=workspace)
    shutil.rmtree(workspace)
    destination = tmp_path / "next"
    assert resources.materialize(conversation, workspace_path=destination) == conversation
    for reference in (reused, next_source, partial, source):
        assert hashlib.sha256((destination / reference.path).read_bytes()).hexdigest() == reference.sha256
    assert not (destination / result.model_display.recovery.path).exists()


def test_new_source_preserves_declared_legacy_result_manifest_but_not_its_results(tmp_path):
    from dagent.harness_runtime.compaction import CompactionRecordV1, CompactionSourceV1

    workspace = tmp_path / "first"
    store = ResultStore(workspace, ".runtime")
    result_body = store.save_text("legacy", "earlier result")
    result = _conversation("").items[1].model_copy(update={"content": result_body})
    legacy, _ = store.archive(None, (result,))
    source = store.snapshot_text(CompactionSourceV1(
        items=(), refs=(legacy,), previous_result_manifest=legacy,
    ).model_dump_json())
    record = store.snapshot_text(CompactionRecordV1(
        status="complete", source=source, source_digest=source.sha256,
        source_ranges=(), chunks=(),
    ).model_dump_json())
    conversation = ConversationState(summary=ContextSummary(
        content="summary", source_item_count=1, compaction_record=record,
    ))
    resources = ConversationResourceStore(tmp_path, ".runtime")
    resources.persist(conversation, workspace_path=workspace)
    shutil.rmtree(workspace)
    destination = tmp_path / "next"
    resources.materialize(conversation, workspace_path=destination)
    rows = (destination / legacy.path).read_text().splitlines()
    assert ToolResultMessage.model_validate_json(rows[0]).content == result_body
    assert not (destination / result_body.path).exists()


@pytest.mark.asyncio
async def test_smaller_window_references_only_the_latest_frozen_atomic_batch(tmp_path):
    from dagent.harness_runtime.context import ContextAssembler
    from dagent.schemas.conversation import UserMessage

    store = ResultStore(tmp_path, ".runtime")
    original = _conversation("large immutable evidence " * 4000)
    original = original.model_copy(update={"items": (
        UserMessage(run_id="run", content="Keep the current task."),
        *(item.model_copy(update={"run_id": "run"}) for item in original.items),
    )})
    frozen = _freeze(original, store, cap=8192)
    assert HeuristicTokenCounter().count_text(frozen.items[-1].model_display.text) > 4096
    prepared = await ContextAssembler(context_window_tokens=4096, max_output_tokens=512).prepare(
        system_message={"content": "instructions"}, conversation=frozen,
        tools=_reader_tools(), policy=ContextPolicy(compaction_strategy="cache_aware"),
        active_run_id="run", result_store=store, enable_new_strategy=True,
    )
    result = prepared.conversation.items[-1]
    assert prepared.conversation.items[:2] == frozen.items[:2]
    assert result.model_display.revision == 1
    assert result.call_id == frozen.items[-1].call_id
    assert prepared.usage.prefix_break_reason == "emergency_result_reference"
    assert prepared.usage.estimated_input_tokens <= prepared.usage.input_budget_tokens
    assert frozen.items[-1].model_display.revision == 0


@pytest.mark.asyncio
async def test_failed_final_gate_keeps_the_original_frozen_receipt(tmp_path):
    from dagent.harness_runtime.context import ContextAssembler
    from dagent.schemas.context import ContextWindowExceeded
    from dagent.schemas.conversation import UserMessage

    store = ResultStore(tmp_path, ".runtime")
    original = _conversation("large immutable evidence " * 1000)
    call = original.items[0].tool_calls[0].model_copy(update={"arguments": {"required": "x" * 20000}})
    original = original.model_copy(update={"items": (
        UserMessage(run_id="run", content="Keep the current task."),
        original.items[0].model_copy(update={"run_id": "run", "tool_calls": (call,)}),
        original.items[1].model_copy(update={"run_id": "run"}),
    )})
    frozen = _freeze(original, store, cap=8192)
    with pytest.raises(ContextWindowExceeded) as raised:
        await ContextAssembler(context_window_tokens=1024, max_output_tokens=128).prepare(
            system_message={"content": "instructions"}, conversation=frozen,
            tools=_reader_tools(), policy=ContextPolicy(compaction_strategy="cache_aware"),
            active_run_id="run", result_store=store, enable_new_strategy=True,
        )
    assert raised.value._conversation == frozen
    assert raised.value._conversation.items[-1].model_display.revision == 0
    assert raised.value._conversation.items[-1].model_display.previous_display is None
