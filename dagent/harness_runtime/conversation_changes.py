"""Run-owned conversation publication, independent of provider event delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from uuid import uuid4

from dagent.schemas.conversation import AssistantMessage, Attachment, ContentReference, ConversationItem, ConversationState
from dagent.schemas.conversation_changes import (
    ConversationChangeBatch, ConversationChangeFailure, ConversationChangeHandler,
    ConversationCompactionStateReplacement, ConversationContextChange,
    ConversationItemPlacement, ConversationMessageChange, ConversationMessageOrigin,
    ConversationObservation, ConversationResourceReady, ConversationSummaryReplacement,
    ConversationResponseObservation, ConversationToolObservation,
)
from dagent.schemas.results import RunState
from dagent.schemas.run_trace import RunTrace


class ConversationDeliveryFailure(Exception):
    def __init__(self, cause: BaseException, failure: ConversationChangeFailure):
        super().__init__(str(cause))
        self.cause = cause
        self.failure = failure
        self.trace: RunTrace | None = None


@dataclass
class ChangeScope:
    publisher: ConversationChangePublisher
    origin: ConversationMessageOrigin
    public: bool = True
    response: ResponseRecorder | None = None
    trace: RunTrace | None = None


_CURRENT: ContextVar[ChangeScope | None] = ContextVar("dagent_conversation_changes", default=None)


def current_changes() -> ChangeScope | None:
    return _CURRENT.get()


@contextmanager
def conversation_change_scope(publisher: ConversationChangePublisher | None) -> Iterator[None]:
    token = _CURRENT.set(None if publisher is None else ChangeScope(
        publisher, ConversationMessageOrigin(id=publisher.delivery_id),
    ))
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def audit_conversation_scope(origin_id: str, scope: str) -> Iterator[None]:
    parent = current_changes()
    audit = None if parent is None else ChangeScope(
        parent.publisher, ConversationMessageOrigin(id=origin_id, scope=scope), False,
    )
    token = _CURRENT.set(audit)
    try:
        yield
    except ConversationDeliveryFailure as error:
        if audit is not None and audit.trace is not None:
            audit.trace.root.status = "failed"
            error.trace = audit.trace
        raise
    finally:
        _CURRENT.reset(token)


@contextmanager
def response_recording_scope(response: ResponseRecorder | None) -> Iterator[None]:
    parent = current_changes()
    token = _CURRENT.set(None if parent is None else replace(parent, response=response))
    try:
        yield
    finally:
        _CURRENT.reset(token)


class ConversationChangePublisher:
    def __init__(self, callback: ConversationChangeHandler, initial: ConversationState | None):
        self.callback = callback
        self.delivery_id = f"delivery_{uuid4().hex}"
        self.conversation = initial.model_copy(deep=True) if initial is not None else None
        self.run_id = ""
        self.kind = "tool"
        self.workspace_path: str | None = None
        self.runtime_directory = ".runtime"
        self.sequence = self.acknowledged_sequence = 0
        self.acknowledged_revision = initial.revision if initial else None
        self.failure: ConversationDeliveryFailure | None = None
        self.state_factory: Callable[[], RunState] | None = None
        self.usage_factory = None
        self._lock = asyncio.Lock()
        self._resource_lock = Lock()
        self._resources: dict[tuple[str, str, str], ConversationResourceReady] = {}
        self._seen_resources: set[tuple[str, str, str]] = set()
        self._messages: dict[tuple[str, str], ConversationMessageChange] = {}
        if self.conversation is not None:
            origin = ConversationMessageOrigin(id=self.conversation.id)
            for item in self.conversation.items:
                self._messages[(origin.id, item.id)] = ConversationMessageChange(item=item, origin=origin)

    def bind(self, run_id: str, kind: str, workspace: str | Path | None,
             runtime_directory: str, initial: ConversationState | None = None) -> None:
        self.run_id, self.kind = run_id, kind
        self.workspace_path = None if workspace is None else str(workspace)
        self.runtime_directory = runtime_directory
        if self.conversation is None and initial is not None:
            self.conversation = initial.model_copy(deep=True)
            self.acknowledged_revision = initial.revision

    def note_resource(self, reference: ContentReference | Attachment,
                      workspace: str | Path, runtime_directory: str) -> None:
        location = str(Path(workspace).resolve())
        key = (location, runtime_directory, reference.path)
        with self._resource_lock:
            if key not in self._seen_resources:
                self._resources[key] = ConversationResourceReady(
                    reference=reference, workspace_path=location, runtime_directory=runtime_directory,
                )

    def recorded_item_ids(self) -> set[str]:
        return {key[1] for key in self._messages}

    def check(self) -> None:
        if self.failure is not None:
            raise self.failure

    async def publish(self, *, state: ConversationState | None = None,
                      messages: Sequence[ConversationMessageChange] = (),
                      observations: Sequence[ConversationObservation] = ()) -> None:
        async with self._lock:
            self.check()
            if not self.run_id:
                raise RuntimeError("Conversation publisher must be bound before publication.")
            before = self.conversation
            context = None
            records = [record.model_copy(deep=True) for record in messages]
            if state is not None:
                state = state.model_copy(deep=True)
                if before is None or before.id != state.id:
                    raise RuntimeError("Published conversation identity does not match its initial state.")
                context, changed = _state_changes(before, state)
                origin = ConversationMessageOrigin(id=state.id)
                records.extend(ConversationMessageChange(item=item, origin=origin) for item in changed)
                self.conversation = state
            after = self.conversation
            # Prefer an explicitly supplied response association over a plain state record.
            selected: dict[tuple[str, str], ConversationMessageChange] = {}
            for record in records:
                key = (record.origin.id, record.item.id)
                previous = self._messages.get(key)
                if previous is not None and record.response_id is None:
                    record = record.model_copy(update={"response_id": previous.response_id})
                if key not in selected:
                    selected[key] = record
            changed_records = tuple(record for key, record in selected.items()
                                    if self._messages.get(key) != record)
            with self._resource_lock:
                resources = tuple(self._resources.values())
                resource_keys = tuple(self._resources)
                self._resources.clear()
            if context is None and not (changed_records or observations or resources):
                return
            self.sequence += 1
            batch = ConversationChangeBatch(
                conversation_schema_version=after.schema_version if after else None,
                batch_id=f"{self.delivery_id}:{self.sequence}", delivery_id=self.delivery_id,
                sequence=self.sequence, conversation_id=after.id if after else None,
                run_id=self.run_id, base_revision=before.revision if before else None,
                revision=after.revision if after else None, context=context,
                messages=changed_records, observations=tuple(observations), resources=resources,
            )
            async def save_batch() -> None:
                # Host-side SDK/file work must not become part of this Run or
                # recursively publish while its delivery lock is held.
                with conversation_change_scope(None):
                    await self.callback(batch.model_copy(deep=True))

            cancelled = False
            try:
                task = asyncio.create_task(save_batch())
                while True:
                    try:
                        await asyncio.shield(task)
                        break
                    except asyncio.CancelledError:
                        if task.done() and task.cancelled():
                            raise
                        cancelled = True
            except (Exception, asyncio.CancelledError) as cause:
                self.failure = ConversationDeliveryFailure(cause, ConversationChangeFailure(
                    failed_batch=batch, acknowledged_sequence=self.acknowledged_sequence,
                    acknowledged_revision=self.acknowledged_revision,
                ))
                raise self.failure from cause
            self._messages.update(selected)
            with self._resource_lock:
                self._seen_resources.update(resource_keys)
            self.acknowledged_sequence = batch.sequence
            self.acknowledged_revision = batch.revision
            if cancelled:
                raise asyncio.CancelledError


def _state_changes(before: ConversationState, after: ConversationState) -> tuple[ConversationContextChange | None, tuple[ConversationItem, ...]]:
    if before == after:
        return None, ()
    old = {item.id: item for item in before.items}
    new = {item.id: item for item in after.items}
    removed = tuple(item.id for item in before.items if item.id not in new)
    old_survivors = tuple(item.id for item in before.items if item.id in new)
    new_survivors = tuple(item.id for item in after.items if item.id in old)
    if old_survivors != new_survivors:
        raise RuntimeError("Conversation changes cannot reorder retained messages.")
    summary = (ConversationSummaryReplacement(value=after.summary, replaced_item_ids=removed)
               if before.summary != after.summary else None)
    context = ConversationContextChange(
        added=tuple(ConversationItemPlacement(item_id=item.id, index=index)
                    for index, item in enumerate(after.items) if item.id not in old),
        updated_item_ids=tuple(item.id for item in after.items
                               if item.id in old and item != old[item.id]),
        deactivated_item_ids=() if summary is not None else removed,
        summary_change=summary,
        compaction_state_change=(ConversationCompactionStateReplacement(value=after.compaction_state)
                                 if before.compaction_state != after.compaction_state else None),
    )
    return context, tuple(item for item in after.items if old.get(item.id) != item)


async def publish_state(state: ConversationState, items: Sequence[ConversationItem] = (),
                        observations: Sequence[ConversationObservation] = ()) -> None:
    scope = current_changes()
    if scope is None:
        return
    origin = (ConversationMessageOrigin(id=state.id) if scope.public else scope.origin)
    await scope.publisher.publish(
        state=state if scope.public else None, observations=observations,
        messages=tuple(ConversationMessageChange(item=item, origin=origin) for item in items),
    )


async def publish_messages(items: Sequence[ConversationItem], *, response_id: str | None = None,
                           partial: bool = False) -> None:
    scope = current_changes()
    if scope is None:
        return
    publisher = scope.publisher
    for item in items:
        origin = scope.origin
        if scope.public:
            origin = ConversationMessageOrigin(
                id=(publisher.conversation.id if publisher.conversation is not None and item.scope == "conversation"
                    else f"{publisher.run_id}:{item.scope}"), scope=item.scope,
            )
        await publisher.publish(messages=(ConversationMessageChange(
            item=item, origin=origin, response_id=response_id, partial=partial,
        ),))


async def flush_resources() -> None:
    scope = current_changes()
    if scope is not None and scope.publisher.run_id:
        await scope.publisher.publish()


def note_resource(reference: ContentReference | Attachment, workspace: str | Path,
                  runtime_directory: str) -> None:
    scope = current_changes()
    if scope is not None:
        scope.publisher.note_resource(reference, workspace, runtime_directory)


class ResponseRecorder:
    """Identity and terminal records for one existing model response path."""

    def __init__(self, *, run_id: str | None, step: int | None = None, scope: str = "conversation"):
        from dagent.harness_runtime.runtime_events import ResponseStreamContext

        self.owner = current_changes()
        self.enabled = self.owner is not None and bool(self.owner.publisher.run_id)
        self.scope = scope
        self.message_id = f"msg_{uuid4().hex}" if self.enabled else None
        self.context = ResponseStreamContext.create(
            run_id=run_id, model_step=step, message_id=self.message_id,
        )
        self.finished = False
        self._observed_content: str | None = None
        self._observed_reasoning: str | None = None

    def origin(self) -> ConversationMessageOrigin:
        assert self.owner is not None
        publisher = self.owner.publisher
        if not self.owner.public:
            return self.owner.origin
        return ConversationMessageOrigin(
            id=(publisher.conversation.id if publisher.conversation is not None and self.scope == "conversation"
                else f"{publisher.run_id}:{self.scope}"), scope=self.scope,
        )

    async def start(self) -> None:
        if self.enabled:
            await self.owner.publisher.publish(observations=(self._observation("started"),))

    def capture(self, output) -> None:
        if output is not None:
            self._observed_content = "".join(output.observed_content) or None
            self._observed_reasoning = "".join(output.observed_reasoning) or None

    def _observation(self, status, item: AssistantMessage | None = None):
        return ConversationResponseObservation(
            message_id=self.message_id, response_id=self.context.response_id,
            origin=self.origin(), status=status,
            observed_content=(self._observed_content if item is not None
                              and self._observed_content != item.content else None),
            observed_reasoning=(self._observed_reasoning if item is not None
                                and self._observed_reasoning != item.reasoning else None),
        )

    async def finish(self, item: AssistantMessage, *, state: ConversationState | None = None,
                     status: str = "completed", partial: bool = False) -> None:
        if not self.enabled or self.finished:
            return
        self.finished = True
        owner = self.owner
        await owner.publisher.publish(
            state=state if owner.public else None,
            messages=(ConversationMessageChange(
                item=item, origin=self.origin(), response_id=self.context.response_id, partial=partial,
            ),), observations=(self._observation(status, item),),
        )

    async def interrupt(self, output, status: str) -> None:
        if self.enabled and not self.finished:
            self.capture(output)
            item = AssistantMessage(
                id=self.message_id, run_id=self.context.run_id,
                content="" if output is None else "".join(output.observed_content),
                reasoning="" if output is None else "".join(output.observed_reasoning),
                scope=self.scope,
                visibility="user" if self.scope == "conversation" else "internal",
            )
            await self.finish(item, status=status, partial=True)


def tool_observations(call_id, capability_id, status, result_item_id=None):
    scope = current_changes()
    if scope is None:
        return ()
    origin = (ConversationMessageOrigin(id=scope.publisher.conversation.id)
              if scope.public and scope.publisher.conversation is not None else scope.origin)
    return (ConversationToolObservation(call_id=call_id, capability_id=capability_id,
                                       origin=origin, status=status, result_item_id=result_item_id),)


async def observe_tool(call_id, capability_id, status) -> None:
    scope = current_changes()
    if scope is not None:
        await scope.publisher.publish(observations=tool_observations(call_id, capability_id, status))
