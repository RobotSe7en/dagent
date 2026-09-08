"""Uploads are mutable working files; retained results are verified snapshots."""

import asyncio
import hashlib
from pathlib import Path

import pytest

import dagent
from dagent.harness_runtime.artifacts import (
    materialize_artifact_uploads,
    materialize_workbench_uploads,
)
from dagent.harness_runtime.conversation_resources import ConversationResourceStore
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import Artifact


def _upload():
    return dagent.ArtifactUpload(filename="note.txt", content=b"hello")


def _change(target: Path, operation: str) -> None:
    if operation == "delete":
        target.unlink()
    else:
        target.write_bytes(b"HELLO" if operation == "same_size" else b"updated contents")


@pytest.mark.parametrize("kind", ["attachment", "artifact"])
@pytest.mark.parametrize("corruption", ["short_write", "truncated", "same_size"])
def test_upload_integrity_is_checked_at_ingestion(tmp_path, monkeypatch, kind, corruption):
    write_bytes = Path.write_bytes

    def corrupt(path, content):
        damaged = b"HELLO" if corruption == "same_size" else content[:-1]
        written = write_bytes(path, damaged)
        return written if corruption == "short_write" else len(content)

    monkeypatch.setattr(Path, "write_bytes", corrupt)
    expected = "SHA-256 mismatch" if corruption == "same_size" else "size mismatch"
    with pytest.raises(OSError, match=expected):
        if kind == "attachment":
            materialize_workbench_uploads([_upload()], workspace_path=tmp_path)
        else:
            materialize_artifact_uploads(
                {"source": [_upload()]},
                artifacts={"source": Artifact(id="source", paths=["note.txt"])},
                workspace_path=tmp_path,
            )


@pytest.mark.parametrize("kind", ["attachment", "artifact"])
def test_empty_and_overwriting_uploads_are_allowed(tmp_path, kind):
    for content in (b"initial", b"", b"replacement"):
        upload = dagent.ArtifactUpload(filename="note.txt", content=content)
        if kind == "attachment":
            materialize_workbench_uploads([upload], workspace_path=tmp_path)
            target = tmp_path / "uploads/note.txt"
        else:
            materialize_artifact_uploads(
                {"source": [upload]},
                artifacts={"source": Artifact(id="source", paths=["note.txt"])},
                workspace_path=tmp_path,
            )
            target = tmp_path / "note.txt"
        assert target.read_bytes() == content


def test_invalid_upload_stops_before_agent_execution(tmp_path, monkeypatch):
    provider = MockProvider([ChatResponse(content="must not execute")])
    monkeypatch.setattr(Path, "write_bytes", lambda self, content: 0)
    runner = dagent.Runner(workspace=tmp_path, provider=provider, skill_roots=[])
    try:
        with pytest.raises(OSError, match="write size mismatch"):
            asyncio.run(runner.run(
                dagent.ToolAgent(profile="conversation", capabilities=[]),
                input="inspect", input_uploads=[_upload()],
            ))
        assert not provider.requests
    finally:
        runner.close()


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("operation", ["same_size", "resize", "delete"])
def test_edited_upload_finishes_and_continues_with_current_file(tmp_path, operation, streaming):
    workspace = tmp_path / "work"
    target = workspace / "uploads/note.txt"

    @dagent.tool
    def change_upload() -> str:
        _change(target, operation)
        return "changed"

    @dagent.tool
    def read_upload() -> str:
        return target.read_text()

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="change", name="tool_change_upload", arguments={})]),
        ChatResponse(content="changed"),
        ChatResponse(tool_calls=[ToolCall(id="read", name="tool_read_upload", arguments={})]),
        ChatResponse(content="finished"),
    ])
    runner = dagent.Runner(workspace=tmp_path, provider=provider, skill_roots=[])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[change_upload, read_upload])

    async def execute():
        kwargs = dict(input="change", input_uploads=[_upload()], workspace_path=workspace)
        if streaming:
            events = [event async for event in runner.stream(agent, **kwargs)]
            assert "run.failed" not in [event.type for event in events]
            first = events[-1].data.result
        else:
            first = await runner.run(agent, **kwargs)
        assert first.status == "completed"
        attachment = first.conversation.items[0].attachments[0]
        assert attachment.byte_length == 5
        assert attachment.sha256 == hashlib.sha256(b"hello").hexdigest()
        second = await runner.run(
            agent, input="read current file", conversation=first.conversation,
            workspace_path=workspace,
        )
        assert second.status == "completed"
        result = next(item for item in second.new_items if isinstance(item, dagent.ToolResultMessage))
        if operation == "delete":
            assert result.status == "failed"
            assert not target.exists()
        else:
            assert result.status == "completed"
            assert target.read_text() in result.content.text
        assert second.conversation.items[0].attachments[0] == attachment
        assert not (workspace / ".runtime/history").exists()

    try:
        asyncio.run(execute())
    finally:
        runner.close()


@pytest.mark.parametrize("operation", ["same_size", "resize", "delete"])
def test_review_and_restart_preserve_upload_edits(tmp_path, operation):
    workspace = tmp_path / "work"
    target = workspace / "uploads/note.txt"
    observed = []

    @dagent.tool
    def edit_before_review() -> str:
        target.write_bytes(b"changed before review")
        return "edited"

    @dagent.tool(risk="medium")
    def edit_after_review() -> str:
        observed.append(target.read_bytes() if target.exists() else None)
        target.write_bytes(b"changed after review")
        return "edited again"

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="before", name="tool_edit_before_review", arguments={})]),
        ChatResponse(tool_calls=[ToolCall(id="after", name="tool_edit_after_review", arguments={})]),
        ChatResponse(content="done"),
    ])
    tools = [edit_before_review, edit_after_review]
    runner = dagent.Runner(workspace=tmp_path, provider=provider, skill_roots=[])
    try:
        pending = asyncio.run(runner.run(
            dagent.ToolAgent(profile="conversation", capabilities=tools, review="careful"),
            input="edit twice", input_uploads=[_upload()], workspace_path=workspace,
        ))
        assert pending.status == "awaiting_review"
        assert target.read_bytes() == b"changed before review"
        checkpoint = dagent.RunCheckpoint.model_validate_json(pending.checkpoint.model_dump_json())
    finally:
        runner.close()
    _change(target, operation)
    expected = target.read_bytes() if target.exists() else None
    resumed_runner = dagent.Runner(
        workspace=tmp_path, provider=provider, capabilities=tools, skill_roots=[],
    )

    async def resume():
        events = [event async for event in resumed_runner.resume_stream(
            pending.review.approve(), checkpoint=checkpoint,
        )]
        assert "run.failed" not in [event.type for event in events]
        assert events[-1].data.result.status == "completed"

    try:
        asyncio.run(resume())
        assert observed == [expected]
        assert target.read_bytes() == b"changed after review"
    finally:
        resumed_runner.close()


def test_only_result_references_are_persisted_and_rebased(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    content = b"retained result"
    source = original / "result.txt"
    source.write_bytes(content)
    reference = dagent.ContentReference(
        path="result.txt", media_type="text/plain", byte_length=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    attachment = dagent.Attachment(
        path="uploads/missing.txt", byte_length=5, sha256=hashlib.sha256(b"hello").hexdigest(),
    )
    conversation = dagent.ConversationState(items=(
        dagent.UserMessage(content="historical upload", attachments=(attachment,)),
        dagent.ToolResultMessage(
            call_id="result", name="tool_result", status="completed", content=reference,
            value_reference=reference, value=reference.model_dump(mode="json"), artifacts=(reference,),
        ),
    ))
    store = ConversationResourceStore(tmp_path, ".runtime")
    store.persist(conversation, workspace_path=original)
    source.unlink()
    destination = tmp_path / "new"
    restored = store.materialize(conversation, workspace_path=destination)
    assert restored.items[0] == conversation.items[0]
    result = restored.items[1]
    assert (destination / result.content.path).read_bytes() == content
    assert result.value_reference == result.content == result.artifacts[0]
    assert result.value == result.content.model_dump(mode="json")
    (destination / result.content.path).write_bytes(b"x" * len(content))
    with pytest.raises(dagent.ConversationResourceError, match="SHA-256 mismatch"):
        store.materialize(restored, workspace_path=destination)
    with pytest.raises(dagent.ConversationResourceError, match="SHA-256 mismatch"):
        store.persist(restored, workspace_path=destination)
