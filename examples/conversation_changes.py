"""Persist change batches, resume a review, and continue in place or elsewhere.

Run: uv run python -m examples.conversation_changes

This journal is example host code, not a persistence layer in the SDK. A real
host should apply each batch and its receipt in its own database transaction.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing, closing
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
        self.sequences: dict[str, int] = {}
        self.terminal: dagent.ConversationChangeBatch | None = None

    async def save(self, batch: dagent.ConversationChangeBatch) -> None:
        if batch.batch_id in self.receipts:
            return
        # Copy notified files before acknowledging the batch. A batch carries
        # only references, so the host reads each file itself. Files are
        # owned by the host: a missing required resource rejects this batch.
        # The SDK copies nothing between workspaces, so these copies are what
        # a host writes back when it moves the conversation (see restore).
        for resource in batch.resources:
            source = self.runner.resolve_runtime_path(
                resource.reference.path, workspace_path=resource.workspace_path,
                runtime_directory=resource.runtime_directory,
            )
            data = await asyncio.to_thread(source.read_bytes)
            target = self.directory / "files" / resource.reference.path
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(target.write_bytes, data)
        await asyncio.to_thread(self._append, batch.model_dump_json())
        self.apply(batch)

    def restore(self, workspace: Path) -> None:
        # Write saved files back to their logical paths in another workspace.
        saved = self.directory / "files"
        for source in saved.rglob("*"):
            if source.is_file():
                target = self.runner.resolve_runtime_path(
                    source.relative_to(saved).as_posix(), workspace_path=workspace,
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())

    def _append(self, line: str) -> None:
        # Keep full batches: terminal tool observations remain useful even
        # when SDK result-file storage fails and no result message is available.
        with self.path.open("a", encoding="utf-8") as journal:
            journal.write(line + "\n")
            journal.flush()
            os.fsync(journal.fileno())

    def apply(self, batch: dagent.ConversationChangeBatch) -> None:
        if batch.batch_id in self.receipts:
            return
        if batch.sequence != self.sequences.get(batch.delivery_id, 0) + 1:
            raise ValueError("Missing or out-of-order persistence batch.")
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
        self.sequences[batch.delivery_id] = batch.sequence
        if batch.status is not None:
            self.terminal = batch
        self.receipts.add(batch.batch_id)

    def reload(self) -> HostJournal:
        restored = HostJournal(self.directory, self.runner)
        for line in self.path.read_text(encoding="utf-8").splitlines():
            restored.apply(dagent.ConversationChangeBatch.model_validate_json(line))
        return restored


async def consume(runner: dagent.Runner, stream, journal: HostJournal) -> dagent.ConversationChangeBatch:
    """Keep reading display events while one writer commits durable batches."""
    pending: asyncio.Queue[dagent.ConversationChangeBatch] = asyncio.Queue(maxsize=1)
    terminal = None

    async def write() -> None:
        while True:
            batch = await pending.get()
            try:
                await journal.save(batch)
            except Exception as exc:
                await runner.reject_changes(batch.run_id, batch.batch_id, message=str(exc))
            else:
                await runner.acknowledge_changes(batch.run_id, batch.batch_id)

    writer = asyncio.create_task(write())
    try:
        async with aclosing(stream):
            async for event in stream:
                if event.type == "conversation.changed":
                    await pending.put(event.data)
                    if event.data.status is not None:
                        terminal = event.data
                elif event.type == "response.content.delta":
                    print(event.data.delta, end="", flush=True)
                elif event.type == "run.failed":
                    raise RuntimeError(f"{event.data.error_type}: {event.data.message}; "
                                       f"last confirmed sequence={event.data.acknowledged_sequence}")
        if terminal is None:
            raise RuntimeError("Stream ended without a confirmed terminal batch.")
        return terminal
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


async def main() -> None:
    @dagent.tool(risk="medium")
    def report() -> str:
        """Produce a report requiring human approval."""
        return "Verified evidence.\n" * 1000

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="report", name="tool_report", arguments={})]),
        ChatResponse(content="The approved report is ready."),
        ChatResponse(content="The earlier report remains available in place."),
        ChatResponse(content="Continued from saved context in a moved workspace."),
    ])
    with TemporaryDirectory(prefix="dagent-changes-") as temporary:
        root = Path(temporary)
        def make_runner():
            return dagent.Runner(
                workspace=root / "workspace", provider=provider, skill_roots=[], capabilities=[report],
                max_retained_runs=0,
                result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024),
            )
        agent = dagent.ToolAgent(profile="conversation", capabilities=[report, "tool.read_file"], review="careful")
        options = dagent.StreamPersistenceOptions()
        with closing(make_runner()) as runner:
            journal = HostJournal(root / "host", runner)
            pending = await consume(runner, runner.stream(agent, input="Prepare a report.", persistence=options), journal)
            assert pending.status == "awaiting_review"
            assert runner.run_checkpoint(pending.run_id) is None

        # A fresh Runner reads the existing public checkpoint from the same
        # journal. No result snapshot or trace is needed.
        with closing(make_runner()) as runner:
            journal.runner = runner
            journal = journal.reload()
            checkpoint = journal.terminal.checkpoint
            runner.validate_checkpoint(checkpoint)
            assert checkpoint.state.trace is None
            assert checkpoint.pending_tool_invocations[0].capability_id == "tool.report"
            completed = await consume(runner, runner.resume_stream(
                checkpoint.review.approve(), checkpoint=checkpoint, persistence=options,
            ), journal)
            assert completed.status == "completed"
            saved = next(item.content for item in journal.state.items if isinstance(item, dagent.ToolResultMessage))

        for workspace in (None, root / "moved"):
            with closing(make_runner()) as runner:
                journal.runner = runner
                restored = journal.reload()
                if workspace is not None:
                    restored.restore(workspace)
                completed = await consume(runner, runner.stream(
                    agent, input="Continue from the saved report.", conversation=restored.state,
                    workspace_path=workspace, persistence=options,
                ), restored)
                assert completed.status == "completed"
                assert restored.reload().state == restored.state
                assert runner.resolve_runtime_path(saved.path, workspace_path=completed.workspace_path).is_file()
                print(f"\nReplayed {len(restored.receipts)} batches; revision={restored.state.revision}")


if __name__ == "__main__":
    asyncio.run(main())
