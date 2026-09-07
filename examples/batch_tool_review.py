"""Deterministic batch editing/review example; no model credentials required.

Run: uv run python -m examples.batch_tool_review
"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


async def main() -> None:
    with TemporaryDirectory(prefix="dagent-batch-review-") as directory:
        workspace = Path(directory)
        target = workspace / "notes.txt"
        target.write_text("draft\ndraft\n", encoding="utf-8")
        provider = MockProvider([
            ChatResponse(tool_calls=[
                ToolCall(id="read", name="tool_read_file", arguments={"path": "notes.txt"}),
                ToolCall(id="edit", name="tool_edit_file", arguments={
                    "path": "notes.txt", "old_string": "draft", "new_string": "ready", "replace_all": True,
                }),
                ToolCall(id="optional_write", name="tool_write_file", arguments={"path": "extra.txt", "content": "optional"}),
                ToolCall(id="shell", name="tool_shell", arguments={"command": "echo batch review completed"}),
            ]),
            ChatResponse(content="Updated both matches; skipped the rejected optional write."),
        ])
        runner = dagent.Runner(workspace=workspace, provider=provider, skill_roots=[])
        try:
            agent = dagent.ToolAgent(profile="conversation", review="careful", capabilities=[
                "tool.read_file", "tool.edit_file", "tool.write_file", "tool.shell",
            ])
            first = await runner.run(agent, input="Update the notes.", workspace_path=workspace)
            assert first.requires_review and first.usage.capability_calls == 0
            for call in first.review.capability_calls:
                print(f"Review {call.invocation_id}: {call.capability_id} ({call.risk})")
            # A host normally obtains these choices from the reviewer.
            decision = first.review.decide([
                dagent.CapabilityReviewDecision(
                    invocation_id=call.invocation_id, approved=call.invocation_id != "optional_write",
                ) for call in first.review.capability_calls
            ])
            checkpoint = dagent.RunCheckpoint.model_validate_json(first.checkpoint.model_dump_json())
            final = await runner.resume(decision, checkpoint=checkpoint)
            assert target.read_text(encoding="utf-8") == "ready\nready\n"
            assert not (workspace / "extra.txt").exists()
            assert final.status == "completed"
            print(final.output_text)
        finally:
            runner.close()


if __name__ == "__main__":
    asyncio.run(main())
