"""Offline external storage: python -m examples.external_runtime_directory."""

import asyncio
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


REPORT = "Retained report row.\n" * 300


@dagent.tool
def report() -> str:
    """Return a report that needs a retained resource."""
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
            result = await first.run(
                dagent.ToolAgent(profile="conversation", capabilities=["tool.report"]),
                input="Produce a report.", workspace_path=first_workspace,
            )
            # The host separately persists this record alongside the private store.
            conversation = dagent.ConversationState.model_validate_json(result.conversation.model_dump_json())
            assert not (first_workspace / ".runtime").exists()
        finally:
            first.close()

        # Host-controlled cleanup; the conversation backing store remains.
        shutil.rmtree(first_workspace)
        shutil.rmtree(private / "runs")
        next_workspace = root / "second"
        second = dagent.Runner(
            workspace=next_workspace, runtime_directory=private,
            provider=MockProvider([ChatResponse(content="Continued with the retained report.")]),
        )
        try:
            continued = await second.run(
                dagent.ToolAgent(profile="conversation"), input="Continue.",
                conversation=conversation, workspace_path=next_workspace,
            )
            item = next(item for item in continued.conversation.items if isinstance(item, dagent.ToolResultMessage))
            reference = item.content
            assert isinstance(reference, dagent.ContentReference)
            physical = second.resolve_runtime_path(reference.path, workspace_path=next_workspace)
            assert physical.read_text() == REPORT
            read = await second.test_capability("tool.read_file", {
                "path": reference.path, "offset_chars": 0, "limit_chars": 40,
            })
            assert read.status == "completed"
            assert not (next_workspace / ".runtime").exists()
            print(continued.output_text)
            print("Logical path:", reference.path)
            print("External file:", physical)
            print("First characters:", read.content)
        finally:
            second.close()


if __name__ == "__main__":
    asyncio.run(main())
