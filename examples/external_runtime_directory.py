"""Offline external storage: python -m examples.external_runtime_directory."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


REPORT = "Retained report row.\n" * 300


@dagent.tool
def report() -> str:
    """Return a report larger than the inline limit."""
    return REPORT


async def main() -> None:
    with TemporaryDirectory(prefix="dagent-external-") as temporary:
        root = Path(temporary)
        private = root / "sdk-data"
        first_workspace = root / "first"
        first = dagent.Runner(
            workspace=first_workspace,
            runtime_directory=private,
            provider=MockProvider([
                ChatResponse(tool_calls=[ToolCall(id="report", name="tool_report", arguments={})]),
                ChatResponse(content="Report saved."),
            ]),
            capabilities=[report],
            result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024),
        )
        try:
            # read_file lets the model open the overflow file, so the result overflows.
            result = await first.run(
                dagent.ToolAgent(profile="conversation", capabilities=["tool.report", "tool.read_file"]),
                input="Produce a report.", workspace_path=first_workspace,
            )
            item = next(item for item in result.conversation.items if isinstance(item, dagent.ToolResultMessage))
            reference = item.content
            assert isinstance(reference, dagent.ContentReference)
            physical = first.resolve_runtime_path(reference.path, workspace_path=first_workspace)
            assert physical.is_relative_to(private)
            assert not (first_workspace / ".runtime").exists()
            read = await first.test_capability("tool.read_file", {
                "path": reference.path, "offset_chars": 0, "limit_chars": 40,
            })
            assert read.status == "completed"
            print("Logical path:", reference.path)
            print("External file:", physical)
            print("First characters:", read.content)
            # The host separately persists this record.
            conversation = dagent.ConversationState.model_validate_json(result.conversation.model_dump_json())
        finally:
            first.close()

        # Overflow files are best-effort and stay in their run workspace's
        # namespace: another workspace keeps the path but does not get the file.
        next_workspace = root / "second"
        second = dagent.Runner(
            workspace=next_workspace, runtime_directory=private,
            provider=MockProvider([ChatResponse(content="Continued in a new workspace.")]),
        )
        try:
            continued = await second.run(
                dagent.ToolAgent(profile="conversation"), input="Continue.",
                conversation=conversation, workspace_path=next_workspace,
            )
            assert reference in [item.content for item in continued.conversation.items
                                 if isinstance(item, dagent.ToolResultMessage)]
            elsewhere = second.resolve_runtime_path(reference.path, workspace_path=next_workspace)
            print(continued.output_text)
            print("Readable in the new workspace:", elsewhere.exists())
        finally:
            second.close()


if __name__ == "__main__":
    asyncio.run(main())
