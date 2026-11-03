"""Persist change batches, resume a review, and continue without result snapshots.

Run: uv run python -m examples.conversation_changes

This journal is example host code, not a persistence layer in the SDK. A real
host should apply each batch and its receipt in its own database transaction.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas.conversation import ConversationItem


class HostJournal:
    def __init__(self, directory: Path, runner: dagent.Runner):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "changes.jsonl"
        self.runner = runner
        self.state: dagent.ConversationState | None = None
        self.records: dict[tuple[str, str], ConversationItem] = {}
        self.receipts: set[str] = set()

    async def save(self, batch: dagent.ConversationChangeBatch) -> None:
        if batch.batch_id in self.receipts:
            return
        # Copy ready files before acknowledging the references. Object writes
        # are idempotent; the existing SDK backing store remains in use too.
        for resource in batch.resources:
            source = self.runner.resolve_runtime_path(
                resource.reference.path, workspace_path=resource.workspace_path,
                runtime_directory=resource.runtime_directory,
            )
            data = await asyncio.to_thread(source.read_bytes)
            if len(data) != resource.reference.byte_length or hashlib.sha256(data).hexdigest() != resource.reference.sha256:
                raise ValueError("Resource identity changed before host storage.")
            await asyncio.to_thread((self.directory / resource.reference.sha256).write_bytes, data)
        await asyncio.to_thread(self._append, batch.model_dump_json())
        self.apply(batch)

    def _append(self, line: str) -> None:
        with self.path.open("a", encoding="utf-8") as journal:
            journal.write(line + "\n")
            journal.flush()
            os.fsync(journal.fileno())

    def apply(self, batch: dagent.ConversationChangeBatch) -> None:
        if batch.batch_id in self.receipts:
            return
        if batch.conversation_id is not None:
            if self.state is None:
                self.state = dagent.ConversationState(id=batch.conversation_id)
            if self.state.revision != batch.base_revision:
                raise ValueError("The batch does not continue the saved conversation revision.")
        for record in batch.messages:
            self.records[(record.origin.id, record.item.id)] = record.item
        change = batch.context
        if change is not None:
            previous = self.state
            summary = previous.summary
            removed = set(change.deactivated_item_ids)
            if change.summary_change is not None:
                summary = change.summary_change.value
                removed.update(change.summary_change.replaced_item_ids)
            items = [item for item in previous.items if item.id not in removed]
            updated = set(change.updated_item_ids)
            items = [self.records[(previous.id, item.id)] if item.id in updated else item for item in items]
            for placement in change.added:
                items.insert(placement.index, self.records[(previous.id, placement.item_id)])
            compaction_state = previous.compaction_state
            if change.compaction_state_change is not None:
                compaction_state = change.compaction_state_change.value
            self.state = dagent.ConversationState(
                id=batch.conversation_id, revision=batch.revision, items=tuple(items),
                summary=summary, compaction_state=compaction_state,
            )
        self.receipts.add(batch.batch_id)

    def reload(self) -> HostJournal:
        restored = HostJournal(self.directory, self.runner)
        for line in self.path.read_text(encoding="utf-8").splitlines():
            restored.apply(dagent.ConversationChangeBatch.model_validate_json(line))
        return restored


async def main() -> None:
    @dagent.tool(risk="medium")
    def report() -> str:
        """Produce a report requiring human approval."""
        return "Verified evidence.\n" * 1000

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="report", name="tool_report", arguments={})]),
        ChatResponse(content="The approved report is ready."),
        ChatResponse(content="The earlier report remains available."),
        ChatResponse(content="The report is available in this workspace too."),
    ])
    with TemporaryDirectory(prefix="dagent-changes-") as temporary:
        root = Path(temporary)
        with closing(dagent.Runner(
            workspace=root / "workspace", provider=provider, skill_roots=[],
            result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024),
        )) as runner:
            journal = HostJournal(root / "host", runner)
            agent = dagent.ToolAgent(profile="conversation", capabilities=[report, "tool.read_file"], review="careful")
            pending = await runner.run(agent, input="Prepare a report.", on_conversation_change=journal.save)
            # A review still needs its separate SDK checkpoint, not a conversation delta.
            checkpoint_path = root / "review.json"
            checkpoint_path.write_text(pending.checkpoint.model_dump_json(), encoding="utf-8")
            checkpoint = dagent.RunCheckpoint.model_validate_json(checkpoint_path.read_text(encoding="utf-8"))
            async for event in runner.resume_stream(
                pending.review.approve(), checkpoint=checkpoint, on_conversation_change=journal.save,
            ):
                if event.type == "run.finished":
                    assert journal.state == event.data.result.conversation

            # Restore twice: the third workspace keeps the rebased logical path
            # but still delivers its new resource location to the host.
            for workspace in (root / "second", root / "third"):
                restored = journal.reload()
                result = await runner.run(
                    agent, input="Continue from the saved report.", conversation=restored.state,
                    workspace_path=workspace, on_conversation_change=restored.save,
                )
                assert restored.state == result.conversation
                assert restored.reload().state == result.conversation
            print(result.output_text)
            print(f"Replayed {len(restored.receipts)} batches; revision={restored.state.revision}")


if __name__ == "__main__":
    asyncio.run(main())
