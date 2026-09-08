"""Upload, edit, and continue in one workspace without model credentials.

Run: uv run python -m examples.editable_uploads
"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


async def main() -> None:
    with TemporaryDirectory(prefix="dagent-editable-upload-") as directory:
        root = Path(directory)
        workspace = root / "work"
        provider = MockProvider([
            ChatResponse(tool_calls=[ToolCall(
                id="edit", name="tool_edit_file", arguments={
                    "path": "uploads/note.txt", "old_string": "draft", "new_string": "revised document",
                },
            )]),
            ChatResponse(content="Edited the uploaded file."),
            ChatResponse(tool_calls=[ToolCall(
                id="read", name="tool_read_file", arguments={"path": "uploads/note.txt"},
            )]),
            ChatResponse(content="Read the revised document in the same workspace."),
        ])
        runner = dagent.Runner(workspace=root, provider=provider, skill_roots=[])
        agent = dagent.ToolAgent(
            profile="conversation", capabilities=["tool.edit_file", "tool.read_file"],
        )
        try:
            first = await runner.run(
                agent, input="Revise the uploaded draft.", workspace_path=workspace,
                input_uploads=[dagent.ArtifactUpload(filename="note.txt", content=b"draft")],
            )
            assert first.status == "completed"
            assert (workspace / "uploads/note.txt").read_text() == "revised document"
            # Historical metadata describes the upload, even after editing.
            assert first.conversation.items[0].attachments[0].byte_length == 5
            second = await runner.run(
                agent, input="Read the current document.",
                conversation=first.conversation, workspace_path=workspace,
            )
            assert second.status == "completed"
            read_result = next(
                item for item in second.new_items if isinstance(item, dagent.ToolResultMessage)
            )
            assert "revised document" in read_result.content.text
            print(second.output_text)
        finally:
            runner.close()


if __name__ == "__main__":
    asyncio.run(main())
