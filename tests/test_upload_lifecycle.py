"""Uploads are mutable working files, written once and never checked again."""

import asyncio
from pathlib import Path

import pytest

import dagent
from dagent.harness_runtime.artifacts import (
    materialize_artifact_uploads,
    materialize_workbench_uploads,
)
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import Artifact


def _upload():
    return dagent.ArtifactUpload(filename="note.txt", content=b"hello")


def _change(target: Path, operation: str) -> None:
    if operation == "delete":
        target.unlink()
    else:
        target.write_bytes(b"HELLO" if operation == "same_size" else b"updated contents")


def _ingest(kind, workspace):
    if kind == "attachment":
        materialize_workbench_uploads([_upload()], workspace_path=workspace)
    else:
        materialize_artifact_uploads(
            {"source": [_upload()]},
            artifacts={"source": Artifact(id="source", paths=["note.txt"])},
            workspace_path=workspace,
        )


@pytest.mark.parametrize("kind", ["attachment", "artifact"])
def test_a_short_upload_write_still_fails(tmp_path, monkeypatch, kind):
    write_bytes = Path.write_bytes
    monkeypatch.setattr(Path, "write_bytes", lambda path, content: write_bytes(path, content[:-1]))
    with pytest.raises(OSError, match="write size mismatch"):
        _ingest(kind, tmp_path)


@pytest.mark.parametrize("kind", ["attachment", "artifact"])
def test_uploads_are_written_once_and_not_read_back(tmp_path, monkeypatch, kind):
    reads = []
    read_bytes = Path.read_bytes

    def counted(path):
        reads.append(path)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted)
    _ingest(kind, tmp_path)
    assert reads == []


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
        # The size is recorded; new uploads carry no digest.
        assert attachment.byte_length == 5 and attachment.sha256 is None
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
