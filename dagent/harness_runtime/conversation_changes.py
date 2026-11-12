"""Run-owned conversation publication, independent of provider event delivery."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from dagent.schemas.conversation import AssistantMessage, Attachment, ContentReference, ConversationItem, ConversationState
from dagent.schemas.conversation_changes import (
    ConversationChangeBatch,
    ConversationCompactionStateReplacement, ConversationContextChange,
    ConversationItemPlacement, ConversationMessageChange, ConversationMessageOrigin,
    ConversationObservation, ConversationResourceReady, ConversationSummaryReplacement,
    ConversationResponseObservation, ConversationToolObservation,
)
from dagent.schemas.results import ExecutionUsage, LoopStatus
from dagent.schemas.run_trace import RunTraceError
from dagent.harness_runtime.stream_delivery import StreamDelivery, PersistenceDeliveryError

if TYPE_CHECKING:
    from dagent.result import RunResult
    from dagent.harness_runtime.execution_usage import ExecutionUsageTracker
    from dagent.harness_runtime.tool_agent import _ToolLoopState


_RESOURCE_CACHE_LIMIT = 128


def _resource_identity(resource: ConversationResourceReady) -> tuple[str, str, str, str, int, str] | None:
    reference = resource.reference
    if not isinstance(reference, ContentReference) or reference.sha256 is None:
        return None
    return (resource.workspace_path, resource.runtime_directory, reference.path,
            reference.sha256, reference.byte_length, reference.media_type)


@dataclass
class ChangeScope:
    publisher: ConversationChangePublisher
    origin: ConversationMessageOrigin
    public: bool = True
    response: ResponseRecorder | None = None
    loop_state: _ToolLoopState | None = None


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
    finally:
        if audit is not None:
            audit.publisher.forget_origin(origin_id)
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
    """Create one durable batch at a time; retain only the active baseline."""

    def __init__(self, delivery: StreamDelivery, initial: ConversationState | None):
        self.delivery = delivery
        self.delivery_id = delivery.delivery_id
        self.conversation = initial.model_copy(deep=True) if initial is not None else None
        self.run_id = ""
        self.kind = "tool"
        self.workspace_path: str | None = None
        self.runtime_directory = ".runtime"
        self.usage_tracker: ExecutionUsageTracker | None = None
        self.loop_state: _ToolLoopState | None = None
        self._lock = asyncio.Lock()
        self._resource_lock = Lock()
        self._confirmed_resources: OrderedDict[tuple[str, str, str, str, int, str], None] = OrderedDict()
        self._resources: dict[tuple[str, str, str], ConversationResourceReady] = {}
        self._messages: dict[tuple[str, str], ConversationMessageChange] = {}
        if self.conversation is not None:
            origin = ConversationMessageOrigin(id=self.conversation.id)
            self._messages = {(origin.id, item.id): ConversationMessageChange(item=item, origin=origin)
                              for item in self.conversation.items}
        delivery.acknowledged_revision = initial.revision if initial else None

    def bind(self, run_id: str, kind: str, workspace: str | Path | None,
             runtime_directory: str, initial: ConversationState | None = None) -> None:
        if kind != "tool":
            raise ValueError("Persistent delivery supports ToolLoop only.")
        self.run_id, self.kind = run_id, kind
        self.delivery.run_id = run_id
        self.workspace_path = None if workspace is None else str(workspace)
        self.runtime_directory = runtime_directory
        if self.conversation is None and initial is not None:
            self.conversation = initial.model_copy(deep=True)
            self.delivery.acknowledged_revision = initial.revision

    def note_resource(self, reference: ContentReference | Attachment,
                      workspace: str | Path, runtime_directory: str) -> None:
        location = str(Path(workspace).resolve())
        key = (location, runtime_directory, reference.path)
        resource = ConversationResourceReady(
            reference=reference, workspace_path=location, runtime_directory=runtime_directory,
        )
        identity = _resource_identity(resource)
        with self._resource_lock:
            if identity is not None and identity in self._confirmed_resources:
                self._confirmed_resources.move_to_end(identity)
                return
            self._resources[key] = resource

    def forget_origin(self, origin_id: str) -> None:
        self._messages = {key: value for key, value in self._messages.items() if key[0] != origin_id}

    def check(self) -> None:
        self.delivery.check()

    async def publish(self, *, state: ConversationState | None = None,
                      messages: Sequence[ConversationMessageChange] = (),
                      observations: Sequence[ConversationObservation] = (),
                      result: RunResult | None = None,
                      status: LoopStatus | Literal["cancelled"] | None = None,
                      error: RunTraceError | None = None,
                      active_origin: tuple[str, set[str]] | None = None) -> None:
        async with self._lock:
            self.check()
            if not self.run_id:
                raise RuntimeError("Conversation publisher must be bound before publication.")
            status = status or (None if result is None else result.state.status)
            before = self.conversation
            context = None
            # Records and states are shared, not copied: the batch below is
            # copied once for the host, and SDK messages are never changed.
            records = list(messages)
            if state is not None:
                if before is None or before.id != state.id:
                    raise RuntimeError("Published conversation identity does not match its initial state.")
                context, changed = _state_changes(before, state)
                origin = ConversationMessageOrigin(id=state.id)
                records.extend(ConversationMessageChange(item=item, origin=origin) for item in changed)
            after = state or self.conversation
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
                resources = tuple(resource for resource in self._resources.values()
                                  if _resource_identity(resource) not in self._confirmed_resources)
                self._resources.clear()
            if context is None and not (changed_records or observations or resources or status):
                return
            output_id = None
            output_text = None if result is None else result.output_text or None
            if output_text and after is not None:
                for item in reversed(after.items):
                    if isinstance(item, AssistantMessage) and (item.content.strip() or item.refusal.strip()) == output_text:
                        output_id, output_text = item.id, None
                        break
            usage = (None if status is None else result.usage if result is not None else
                     self.usage_tracker.snapshot() if self.usage_tracker is not None else ExecutionUsage())
            terminal_error = error or (None if result is None else result.error)
            if status == "failed" and terminal_error is None:
                terminal_error = RunTraceError(code="SdkRunFailed", message="SDK run failed.")
            self.delivery.sequence += 1
            batch = ConversationChangeBatch(
                schema_version=2,
                conversation_schema_version=after.schema_version if after else None,
                batch_id=f"{self.delivery_id}:{self.delivery.sequence}", delivery_id=self.delivery_id,
                sequence=self.delivery.sequence, conversation_id=after.id if after else None,
                run_id=self.run_id, base_revision=before.revision if before else None,
                revision=after.revision if after else None, context=context,
                messages=changed_records, observations=tuple(observations), resources=resources,
                status=status, usage=usage,
                context_usage=None if status is None else () if result is None else result.context_usage,
                error=terminal_error, workspace_path=self.workspace_path if status is not None else None,
                output_item_id=output_id, output_text=output_text,
                checkpoint=result.checkpoint if result is not None and status == "awaiting_review" else None,
            )
            cancelled = await self.delivery.publish(batch)
            self.conversation = after
            self._messages.update(selected)
            with self._resource_lock:
                for resource in resources:
                    identity = _resource_identity(resource)
                    if identity is not None:
                        self._confirmed_resources[identity] = None
                        self._confirmed_resources.move_to_end(identity)
                        if len(self._confirmed_resources) > _RESOURCE_CACHE_LIMIT:
                            self._confirmed_resources.popitem(last=False)
            if state is not None:
                active = {item.id for item in state.items}
                self._messages = {key: value for key, value in self._messages.items()
                                  if key[0] != state.id or key[1] in active}
            if active_origin is not None:
                origin_id, active = active_origin
                self._messages = {key: value for key, value in self._messages.items()
                                  if key[0] != origin_id or key[1] in active}
            if cancelled and batch.status is None:
                raise asyncio.CancelledError

    async def finish(self, result: RunResult | None = None, *,
                     status: LoopStatus | Literal["cancelled"] | None = None,
                     error: RunTraceError | None = None) -> None:
        if result is None and status is None:
            raise ValueError("Terminal publication requires a result or status.")
        await self.publish(state=self.conversation if result is None else result.conversation,
                           result=result, status=status, error=error)


def _changed(previous: ConversationItem, item: ConversationItem) -> bool:
    # Unchanged messages are the same objects; only replaced ones are compared.
    return previous is not item and previous != item


def _state_changes(before: ConversationState, after: ConversationState) -> tuple[ConversationContextChange | None, tuple[ConversationItem, ...]]:
    if before is after or before == after:
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
                               if item.id in old and _changed(old[item.id], item)),
        deactivated_item_ids=() if summary is not None else removed,
        summary_change=summary,
        compaction_state_change=(ConversationCompactionStateReplacement(value=after.compaction_state)
                                 if before.compaction_state != after.compaction_state else None),
    )
    return context, tuple(item for item in after.items if item.id not in old or _changed(old[item.id], item))


async def publish_state(state: ConversationState, items: Sequence[ConversationItem] = (),
                        observations: Sequence[ConversationObservation] = ()) -> None:
    scope = current_changes()
    if scope is None:
        return
    origin = (ConversationMessageOrigin(id=state.id) if scope.public else scope.origin)
    await scope.publisher.publish(
        state=state if scope.public else None, observations=observations,
        messages=tuple(ConversationMessageChange(item=item, origin=origin) for item in items),
        active_origin=None if scope.public else (origin.id, {item.id for item in state.items}),
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

    def _observation(self, status, item: AssistantMessage | None = None, error=None):
        return ConversationResponseObservation(
            message_id=self.message_id, response_id=self.context.response_id,
            origin=self.origin(), status=status, error=error,
            observed_content=(self._observed_content if item is not None
                              and self._observed_content != item.content else None),
            observed_reasoning=(self._observed_reasoning if item is not None
                                and self._observed_reasoning != item.reasoning else None),
        )

    async def finish(self, item: AssistantMessage, *, state: ConversationState | None = None,
                     status: str = "completed", partial: bool = False, error=None) -> None:
        if not self.enabled or self.finished:
            return
        self.finished = True
        owner = self.owner
        try:
            await owner.publisher.publish(
                state=state if owner.public else None,
                messages=(ConversationMessageChange(
                    item=item, origin=self.origin(), response_id=self.context.response_id, partial=partial,
                ),), observations=(self._observation(status, item, error),),
            )
        finally:
            self._observed_content = self._observed_reasoning = None

    async def interrupt(self, output, status: str, *, error: BaseException | None = None) -> None:
        if self.enabled and not self.finished:
            self.capture(output)
            item = AssistantMessage(
                id=self.message_id, run_id=self.context.run_id,
                content=self._observed_content or "",
                reasoning=self._observed_reasoning or "",
                scope=self.scope,
                visibility="user" if self.scope == "conversation" else "internal",
            )
            failure = (RunTraceError(code=type(error).__name__, message=str(error))
                       if error is not None and not isinstance(error, asyncio.CancelledError) else None)
            await self.finish(item, status=status, partial=True, error=failure)


def tool_observations(call_id, capability_id, status, result_item_id=None, *, error=None, retention=None):
    scope = current_changes()
    if scope is None:
        return ()
    origin = (ConversationMessageOrigin(id=scope.publisher.conversation.id)
              if scope.public and scope.publisher.conversation is not None else scope.origin)
    state = scope.loop_state or scope.publisher.loop_state
    conversation = state.conversation if state is not None else scope.publisher.conversation
    assistant = next((item for item in reversed(conversation.items)
                      if isinstance(item, AssistantMessage) and any(call.id == call_id for call in item.tool_calls)), None) if conversation else None
    return (ConversationToolObservation(assistant_message_id=None if assistant is None else assistant.id,
                                       error=error, retention=retention,
                                       call_id=call_id, capability_id=capability_id,
                                       origin=origin, status=status, result_item_id=result_item_id),)


async def observe_tool(call_id, capability_id, status, **details) -> None:
    scope = current_changes()
    if scope is not None:
        await scope.publisher.publish(observations=tool_observations(call_id, capability_id, status, **details))


def repair_open_tool_group(state: ConversationState) -> ConversationState:
    """Deactivate an unfinished tail group; never invent or replay a result."""
    from dagent.schemas.conversation import ToolResultMessage
    index = next((i for i in range(len(state.items) - 1, -1, -1)
                  if isinstance(state.items[i], AssistantMessage)), None)
    if index is None:
        if any(isinstance(item, ToolResultMessage) for item in state.items):
            raise ValueError("Orphan tool result in conversation.")
        return state
    if not state.items[index].tool_calls:
        if any(isinstance(item, ToolResultMessage) for item in state.items[index + 1:]):
            raise ValueError("Orphan tool result in conversation.")
        return state
    assistant = state.items[index]
    expected = {call.id for call in assistant.tool_calls}
    results = [item for item in state.items[index + 1:] if isinstance(item, ToolResultMessage)]
    if len({item.call_id for item in results}) != len(results):
        raise ValueError("Duplicate tool results in conversation.")
    if any(item.call_id not in expected for item in results):
        raise ValueError("Orphan tool result in conversation.")
    if len({item.call_id for item in results}) == len(expected):
        return state
    removed = {assistant.id, *(item.id for item in results)}
    return state.model_copy(update={"items": tuple(item for item in state.items if item.id not in removed),
                                    "revision": state.revision + 1})
