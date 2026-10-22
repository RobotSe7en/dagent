"""Frozen displays preserve the first budget decision and portable recovery."""

import hashlib
import json
import shutil

import pytest

from dagent.harness_runtime.context import HeuristicTokenCounter
from dagent.harness_runtime.conversation_resources import ConversationResourceError, ConversationResourceStore
from dagent.harness_runtime.frozen_results import (
    RecoveryAccess,
    freeze_result_displays,
    reference_frozen_result_displays,
)
from dagent.harness_runtime.result_projection import ResultBudgetExceeded, project_result
from dagent.harness_runtime.result_storage import ResultStore
from dagent.schemas.common import Boundary
from dagent.schemas.context import ContextPolicy
from dagent.schemas.conversation import (
    AssistantMessage, ContextSummary, ConversationState, InlineContent,
    FrozenResultDisplay, ToolCallItem, ToolResultMessage,
)
from dagent.schemas.retention import ResultRetention, ResultCursor


def _conversation(*texts):
    return ConversationState(items=(
        AssistantMessage(tool_calls=tuple(ToolCallItem(id=str(i), name="tool_data") for i in range(len(texts)))),
        *(ToolResultMessage(call_id=str(i), name="tool_data", status="completed", content=InlineContent(text=text))
          for i, text in enumerate(texts)),
    ))


def _freeze(conversation, store, budget=2**60, *, access=None, cap=128):
    return freeze_result_displays(
        conversation, result_store=store, policy=ContextPolicy(max_tool_result_tokens=cap),
        counter=HeuristicTokenCounter(), recovery_access=access or RecoveryAccess(store, True),
        total_budget_tokens=budget,
    )


def _reader_tools():
    return [{"type": "function", "function": {
        "name": "tool_read_file", "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "offset_chars": {"type": "integer"},
            "limit_chars": {"type": "integer"},
        }},
    }}]


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
        assert store.read_reference(display.recovery).decode() == original
        for start, end in display.displayed_spans:
            assert original[start:end] in display.text
        assert "offset_chars" in display.text
        assert "TRUNCATED" in display.text
    assert frozen.items[0] == _conversation(*sources).items[0].model_copy(update={"id": frozen.items[0].id})


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


@pytest.mark.parametrize("access_kind", ["disabled", "denied", "lookalike"])
def test_no_shortening_without_actual_checked_recovery(tmp_path, access_kind):
    store = ResultStore(tmp_path, ".runtime", read_boundary=(
        Boundary(allowed_paths=["allowed"]) if access_kind == "denied" else None
    ))
    access = RecoveryAccess.from_tools(store, [{"type": "function", "function": {
        "name": "tool_not_read_file" if access_kind == "lookalike" else "tool_read_file",
        "parameters": {"properties": {"path": {}, "offset_chars": {}, "limit_chars": {}}},
    }}]) if access_kind != "disabled" else RecoveryAccess(store, False)
    original = "important data " * 1000
    frozen = _freeze(_conversation(original), store, access=access, cap=64)
    assert original in frozen.items[1].model_display.text
    with pytest.raises(ResultBudgetExceeded):
        _freeze(_conversation(original), store, budget=512, access=access, cap=64)


def test_nul_text_cannot_be_hidden_behind_an_unreadable_file_window(tmp_path):
    from dagent.capabilities.tools.file_tools import read_file

    store = ResultStore(tmp_path, ".runtime")
    source = "a" * 5000 + "\0" + "b" * 5000
    reference = store.snapshot_text(source)
    access = RecoveryAccess(store, True)
    assert not access.can_read(reference)
    with pytest.raises(ValueError, match="not a UTF-8 text file"):
        read_file(tmp_path / reference.path, offset_chars=4900, limit_chars=200)
    frozen = _freeze(_conversation(source), store, access=access, cap=64)
    assert source in frozen.items[1].model_display.text
    assert frozen.items[1].model_display.displayed_spans == ((0, len(source)),)
    with pytest.raises(ResultBudgetExceeded):
        _freeze(_conversation(source), store, budget=512, access=access, cap=64)
    with pytest.raises(ResultBudgetExceeded, match="unreadable recovery reference"):
        reference_frozen_result_displays(
            frozen, result_store=store, recovery_access=access, result_ids={frozen.items[1].id},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("source,tools", [("start" * 1000 + "\0" + "end" * 1000, _reader_tools()),
                                          ("Unicode文字🙂" * 1000, [])])
@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("has_spans", [False, True])
async def test_old_frozen_receipt_requires_recovery_only_for_hidden_body(tmp_path, source, tools, complete, has_spans):
    from dagent.harness_runtime.context import ContextAssembler

    store = ResultStore(tmp_path, ".runtime")
    conversation = _conversation(source)
    result = conversation.items[1]
    reference = store.snapshot_text(source)
    body = source if complete else source[:10] + "\n…\n" + source[-10:]
    spans = (((0, len(source)),) if complete else ((0, 10), (len(source) - 10, len(source)))) if has_spans else ()
    # Metadata labels do not determine whether the original result is present.
    # This represents receipts saved before the recovery validation was added.
    display = FrozenResultDisplay(
        text="[status=completed]\n[TRUNCATED] historical metadata\n" + body,
        recovery=reference, displayed_spans=spans,
    )
    result = result.model_copy(update={"content": reference, "model_display": display})
    conversation = conversation.model_copy(update={"items": (conversation.items[0], result)})
    assembler = ContextAssembler(context_window_tokens=131072, max_output_tokens=8192)
    options = dict(system_message={"content": "instructions"}, conversation=conversation,
                   result_store=store, tools=tools, enable_new_strategy=True)
    if complete:
        prepared = await assembler.prepare(policy=ContextPolicy(compaction_strategy="cache_aware"), **options)
        assert prepared.conversation.items[1].model_display == display
        assert source in prepared.request.items[-1].content
    else:
        with pytest.raises(ResultBudgetExceeded, match="undisplayed text.*recovery reference is unavailable") as caught:
            await assembler.prepare(policy=ContextPolicy(compaction_strategy="cache_aware"), **options)
        assert caught.value._conversation == conversation
    # Legacy projection and serialized receipts remain unchanged.
    legacy = await assembler.prepare(policy=ContextPolicy(), **options)
    assert legacy.conversation.items[1].model_display == display
    assert legacy.request.items[-1].content == display.text


@pytest.mark.asyncio
async def test_empty_frozen_body_needs_no_recovery_capability(tmp_path):
    from dagent.harness_runtime.context import ContextAssembler

    store = ResultStore(tmp_path, ".runtime")
    frozen = _freeze(_conversation(""), store, access=RecoveryAccess(store, False))
    prepared = await ContextAssembler().prepare(
        system_message={"content": "instructions"}, conversation=frozen,
        result_store=store, policy=ContextPolicy(compaction_strategy="cache_aware"), enable_new_strategy=True,
    )
    assert prepared.conversation.items[1].model_display == frozen.items[1].model_display


@pytest.mark.parametrize("data,readable", [
    (b"a" * 65535 + "🙂".encode() + b"\r\n", True),
    (b"\xef\xbb\xbf" + "Unicode文字".encode(), True),
    (b"a" * 65535 + b"\xf0\x9f", False),
    (b"a" * 65535 + b"\0", False),
])
def test_text_recovery_validation_shares_the_integrity_scan(tmp_path, monkeypatch, data, readable):
    from pathlib import Path
    from dagent.schemas.conversation import ContentReference

    path = tmp_path / "source.txt"
    path.write_bytes(data)
    reference = ContentReference(path="source.txt", byte_length=len(data), sha256=hashlib.sha256(data).hexdigest())
    store = ResultStore(tmp_path, ".runtime")
    access = RecoveryAccess(store, True)
    opens = []
    original_open = Path.open

    def count_open(target, *args, **kwargs):
        if target == path and args and args[0] == "rb":
            opens.append(target)
        return original_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", count_open)
    assert access.can_read(reference) is readable
    assert RecoveryAccess(store, True).can_read(reference) is readable
    assert len(opens) == 1
    path.write_bytes(b"changed")
    assert not access.can_read(reference)
    assert len(opens) == 2


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
    emergency = reference_frozen_result_displays(
        frozen, result_store=store, recovery_access=RecoveryAccess(store, True), result_ids={result.id},
    )
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
def test_received_window_recovers_from_snapshot_while_source_cursor_skips_received_text(tmp_path, completeness):
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


def test_emergency_reference_is_one_way_and_archives_prior_display(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    frozen = _freeze(_conversation("original " * 1000), store)
    result = frozen.items[1]
    emergency = reference_frozen_result_displays(
        frozen, result_store=store, recovery_access=RecoveryAccess(store, True), result_ids={result.id},
    )
    changed = emergency.items[1]
    assert changed.call_id == result.call_id and changed.status == result.status
    assert emergency.items[0] == frozen.items[0]
    assert changed.model_display.revision == 1
    assert changed.model_display.displayed_spans == ()
    assert store.read_reference(changed.model_display.previous_display).decode() == result.model_display.text
    assert changed.model_display.recovery == result.model_display.recovery
    assert reference_frozen_result_displays(
        emergency, result_store=store, recovery_access=RecoveryAccess(store, True), result_ids={result.id},
    ) is emergency


def test_snapshot_is_immutable_and_verified(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    reference = store.snapshot_text("original")
    assert store.snapshot_text("original") == reference
    (tmp_path / reference.path).write_text("changed!")
    with pytest.raises(OSError, match="SHA-256 mismatch"):
        store.read_reference(reference)
    with pytest.raises(OSError, match="conflicts"):
        store.snapshot_text("original")
    assert not RecoveryAccess(store, True).can_read(reference)


def test_frozen_paths_and_old_display_restore_into_a_new_run(tmp_path):
    workspace = tmp_path / "first"
    result_store = ResultStore(workspace, ".runtime")
    frozen = _freeze(_conversation("source " * 1000), result_store)
    frozen = reference_frozen_result_displays(
        frozen, result_store=result_store, recovery_access=RecoveryAccess(result_store, True),
        result_ids={frozen.items[1].id},
    )
    resources = ConversationResourceStore(tmp_path, ".runtime")
    resources.persist(frozen, workspace_path=workspace)
    shutil.rmtree(workspace)
    destination = tmp_path / "next"
    restored = resources.materialize(frozen, workspace_path=destination)
    assert restored == frozen
    display = restored.items[1].model_display
    assert (destination / display.recovery.path).read_text() == "source " * 1000
    assert (destination / display.previous_display.path).is_file()


def test_repeated_run_boundaries_reread_only_changed_history(tmp_path, monkeypatch):
    import pathlib

    workspace = tmp_path / "first"
    result_store = ResultStore(workspace, ".runtime")
    frozen = _freeze(_conversation("source " * 1000, "other " * 1000), result_store)
    recovery = frozen.items[1].model_display.recovery
    reads = []
    original = pathlib.Path.read_bytes

    def counted(self):
        reads.append(self.name)
        return original(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", counted)
    resources = ConversationResourceStore(tmp_path, ".runtime")
    destination = tmp_path / "next"
    resources.persist(frozen, workspace_path=workspace)
    resources.materialize(frozen, workspace_path=destination)
    first = len(reads)
    assert first
    reads.clear()
    resources.persist(frozen, workspace_path=workspace)
    resources.materialize(frozen, workspace_path=destination)
    assert reads == []  # Unchanged, already verified history is not reread.
    # A changed file is verified again, so corruption is still detected.
    (destination / recovery.path).write_text("broken")
    with pytest.raises(ConversationResourceError, match="size mismatch"):
        resources.materialize(frozen, workspace_path=destination)
    (workspace / recovery.path).write_text("broken")
    with pytest.raises(ConversationResourceError, match="size mismatch"):
        resources.persist(frozen, workspace_path=workspace)


def test_result_json_is_not_interpreted_as_a_resource_manifest(tmp_path):
    store = ResultStore(tmp_path / "first", ".runtime")
    source = json.dumps({"type": "dagent_compaction_source", "refs": [{"path": "must-not-read"}]})
    frozen = _freeze(_conversation(source), store)
    resources = ConversationResourceStore(tmp_path, ".runtime")
    resources.persist(frozen, workspace_path=store.workspace)
    restored = resources.materialize(frozen, workspace_path=tmp_path / "next")
    assert restored.items[1].model_display.text == frozen.items[1].model_display.text


def test_recovery_access_requires_character_cursor_parameters(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    reference = store.snapshot_text("snapshot")
    wrong = [{"type": "function", "function": {
        "name": "tool_read_file", "parameters": {"properties": {"path": {}}},
    }}]
    assert not RecoveryAccess.from_tools(store, wrong).can_read(reference)
    correct = [{"type": "function", "function": {
        "name": "tool_read_file", "parameters": {"properties": {
            "path": {}, "offset_chars": {}, "limit_chars": {},
        }},
    }}]
    assert RecoveryAccess.from_tools(store, correct).can_read(reference)


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


def test_recovery_verification_reuses_only_unchanged_files_within_prepare(tmp_path, monkeypatch):
    from pathlib import Path

    store = ResultStore(tmp_path, ".runtime")
    reference = store.snapshot_text("original")
    target = tmp_path / reference.path
    opens = []
    original_open = Path.open

    def count_open(path, *args, **kwargs):
        if path == target and args and args[0] == "rb":
            opens.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", count_open)
    first = RecoveryAccess(store, True)
    assert first.can_read(reference) and first.can_read(reference)
    assert RecoveryAccess(store, True).can_read(reference)
    assert len(opens) == 1
    assert RecoveryAccess(ResultStore(tmp_path, ".runtime"), True).can_read(reference)
    assert len(opens) == 2
    target.write_bytes(b"modified")
    assert not first.can_read(reference)
    assert len(opens) == 3


def test_compaction_record_restores_previous_chain_and_declared_source_refs(tmp_path):
    from dagent.harness_runtime.compaction import CompactionRecordV1, CompactionSourceV1

    workspace = tmp_path / "first"
    store = ResultStore(workspace, ".runtime")
    frozen = _freeze(_conversation("first " * 1000), store)
    result = frozen.items[1]
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
    for reference in (latest, next_source, previous, source, result.model_display.recovery):
        data = (destination / reference.path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == reference.sha256
    (destination / result.model_display.recovery.path).write_text("broken")
    with pytest.raises(ConversationResourceError, match="size mismatch"):
        resources.materialize(conversation, workspace_path=destination)


def test_failed_record_and_reused_partial_record_restore_their_source_closure(tmp_path):
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
    for reference in (reused, next_source, partial, source, result.model_display.recovery):
        assert hashlib.sha256((destination / reference.path).read_bytes()).hexdigest() == reference.sha256


def test_new_source_preserves_declared_legacy_result_manifest_and_children(tmp_path):
    from dagent.harness_runtime.compaction import CompactionRecordV1, CompactionSourceV1

    workspace = tmp_path / "first"
    store = ResultStore(workspace, ".runtime")
    result_body = store.save_text("legacy", "earlier result")
    result = _conversation("").items[1].model_copy(update={"content": result_body})
    legacy = store.save_text("legacy", result.model_dump_json() + "\n", field="manifest")
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
    assert (destination / result_body.path).read_text() == "earlier result"


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


@pytest.mark.asyncio
async def test_first_display_storage_failure_exposes_complete_failed_sdk_snapshot(tmp_path, monkeypatch):
    from contextlib import closing
    import dagent
    from dagent.providers import ToolCall
    from tests.test_cache_usage import _completion, _usage
    from tests.test_reasoning_mapping import _endpoint

    @dagent.tool
    def evidence() -> str:
        """Return received evidence."""
        return "preserve all received evidence " * 100

    def respond(request):
        return _completion("chat_completions", False, usage=_usage("chat_completions"),
                           content="", calls=(ToolCall(id="call", name="tool_evidence", arguments={}),))

    def fail_snapshot(self, text):
        raise OSError("snapshot disk full")

    monkeypatch.setattr(ResultStore, "snapshot_text", fail_snapshot)
    async with _endpoint(protocol="chat_completions", token_counting="heuristic",
                         max_output_tokens=512, responder=respond) as (provider, posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            with pytest.raises(OSError, match="snapshot disk full") as raised:
                await runner.run(dagent.ToolAgent(
                    profile="conversation", capabilities=[evidence],
                    context=ContextPolicy(compaction_strategy="chunked"),
                ), input="Collect evidence.")
            result = raised.value.result
            assert result.status == "failed" and result.checkpoint is not None
            assert result.conversation.items[-1].call_id == "call"
            assert result.conversation.items[-1].content.text == "preserve all received evidence " * 100
            assert result.conversation.items[-1].model_display is None
            assert len(posts) == 1
