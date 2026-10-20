"""Choose model result displays once, retaining immutable recovery snapshots."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import os
from typing import Any

from dagent.capabilities.tools.boundary import BoundaryViolation, enforce_path_allowed
from dagent.harness_runtime.result_projection import (
    ResultBudgetExceeded,
    ResultItem,
    TextCounter,
    project_result,
)
from dagent.harness_runtime.result_storage import ResultStore
from dagent.schemas.context import ContextPolicy
from dagent.schemas.conversation import (
    ContentReference,
    ConversationState,
    FrozenResultDisplay,
    InlineContent,
    ResultObservation,
    ToolResultMessage,
    UserMessage,
)


@dataclass(frozen=True)
class RecoveryAccess:
    """Actual file-tool availability and its existing checked read boundary."""

    result_store: ResultStore
    read_file_enabled: bool = False

    @classmethod
    def from_tools(cls, result_store: ResultStore, tools: Sequence[dict[str, Any]]) -> RecoveryAccess:
        enabled = False
        for tool in tools:
            function = tool.get("function", tool)
            properties = function.get("parameters", {}).get("properties", {})
            if function.get("name") == "tool_read_file" and {
                "path", "offset_chars", "limit_chars",
            }.issubset(properties):
                enabled = True
                break
        return cls(result_store, read_file_enabled=enabled)

    def can_read(self, reference: ContentReference, *, received_text: str | None = None) -> bool:
        if not self.read_file_enabled:
            return False
        if received_text is not None and "\0" in received_text:
            return False
        store = self.result_store
        try:
            path = (store.workspace / reference.path).resolve()
            path.relative_to(store.workspace)
            if store.read_boundary is not None:
                enforce_path_allowed(reference.path, store.read_boundary, store.workspace)
            if not path.is_file() or not os.access(path, os.R_OK):
                return False
            if not store.is_text_reference(reference):
                return False
        except (BoundaryViolation, ValueError, OSError):
            return False
        return True


def _results(conversation: ConversationState) -> list[tuple[str, ResultItem]]:
    results: list[tuple[str, ResultItem]] = []
    for item in conversation.items:
        if isinstance(item, ToolResultMessage):
            results.append((item.id, item))
        elif isinstance(item, UserMessage):
            results.extend((f"{item.id}/{result.id}", result) for result in item.result_observations)
    return results


def _replace_results(conversation: ConversationState, updates: dict[str, ResultItem]) -> ConversationState:
    if not updates:
        return conversation
    items = []
    for item in conversation.items:
        if isinstance(item, ToolResultMessage):
            item = updates.get(item.id, item)
        elif isinstance(item, UserMessage) and item.result_observations:
            item = item.model_copy(update={"result_observations": tuple(
                updates.get(f"{item.id}/{result.id}", result) for result in item.result_observations
            )})
        items.append(item)
    return conversation.model_copy(update={"items": tuple(items), "revision": conversation.revision + 1})


def validate_frozen_result_recovery(
    conversation: ConversationState,
    recovery_access: RecoveryAccess | None,
) -> None:
    """Reject hidden historical evidence when its recovery is unavailable.

    A complete raw result is the display's suffix, so its stored byte length
    and digest prove completeness without parsing metadata or rereading the
    snapshot. This also handles old receipts without displayed spans.
    """

    for identity, item in _results(conversation):
        display = item.model_display
        if display is None or (recovery_access is not None and recovery_access.can_read(display.recovery)):
            continue
        shown = display.text.encode("utf-8")
        length = display.recovery.byte_length
        body = shown[-length:] if length else b""
        if len(shown) >= length and hashlib.sha256(body).hexdigest() == display.recovery.sha256:
            continue
        raise ResultBudgetExceeded(
            f"Frozen result {identity} has undisplayed text but its recovery reference is unavailable."
        )


def _display_item(item: ResultItem, text: str, recovery: ContentReference) -> ResultItem:
    updates: dict[str, Any] = {"content": InlineContent(text=text), "model_display": None}
    if isinstance(item, ToolResultMessage):
        updates["artifacts"] = (recovery, *item.artifacts)
    else:
        updates["references"] = (recovery, *item.references)
    return item.model_copy(update=updates)


def freeze_result_displays(
    conversation: ConversationState,
    *,
    result_store: ResultStore,
    policy: ContextPolicy,
    counter: TextCounter,
    recovery_access: RecoveryAccess,
    total_budget_tokens: int,
) -> ConversationState:
    """Water-fill a new result batch; existing displays are never redistributed.

    ``total_budget_tokens`` belongs only to displays without a receipt. The
    caller has already reserved fixed input, existing displays and model output.
    """

    pending = [(identity, item) for identity, item in _results(conversation) if item.model_display is None]
    if not pending:
        return conversation
    candidates = []
    for identity, item in pending:
        text = (result_store.read_reference(item.content).decode("utf-8")
                if isinstance(item.content, ContentReference) else item.content.text)
        recovery = result_store.snapshot_text(text)
        readable = recovery_access.can_read(recovery, received_text=text)
        shown = _display_item(item, text, recovery)
        minimum = project_result(
            shown, 2**60, counter, read_available=readable,
            max_body_chars=0 if readable else None, recovery_reference=recovery,
        )
        floor = counter.count_text(minimum.text)
        wanted = project_result(
            shown, floor + policy.max_tool_result_tokens, counter,
            read_available=readable, recovery_reference=recovery,
        ) if readable else minimum
        wanted_tokens = counter.count_text(wanted.text)
        # A complete tiny result can cost less than a reference-only display,
        # whose truncation notice is deliberately explicit.
        floor = min(floor, wanted_tokens)
        candidates.append((identity, item, shown, recovery, readable, floor, wanted_tokens))

    floors = [candidate[5] for candidate in candidates]
    if sum(floors) > total_budget_tokens:
        raise ResultBudgetExceeded(
            f"Minimum information for {len(candidates)} new results requires {sum(floors)} tokens; "
            f"only {total_budget_tokens} are available.", minimum_tokens=sum(floors),
        )
    allocations = list(floors)
    remaining = total_budget_tokens - sum(allocations)
    active = [i for i, candidate in enumerate(candidates) if candidate[6] > allocations[i]]
    while remaining and active:
        share = max(1, remaining // len(active))
        for i in active:
            added = min(share, candidates[i][6] - allocations[i], remaining)
            allocations[i] += added
            remaining -= added
        active = [i for i in active if allocations[i] < candidates[i][6]]

    updates = {}
    for candidate, allocation in zip(candidates, allocations):
        identity, item, shown, recovery, readable, _, _ = candidate
        projection = project_result(
            shown, allocation, counter, read_available=readable, recovery_reference=recovery,
        )
        display = FrozenResultDisplay(
            text=projection.text, recovery=recovery, displayed_spans=projection.displayed_spans,
        )
        updates[identity] = item.model_copy(update={"content": recovery, "model_display": display})
    return _replace_results(conversation, updates)


def reference_frozen_result_displays(
    conversation: ConversationState,
    *,
    result_store: ResultStore,
    recovery_access: RecoveryAccess,
    result_ids: set[str],
) -> ConversationState:
    """Make the explicit one-way emergency transition, preserving the old text."""

    updates = {}
    for identity, item in _results(conversation):
        display = item.model_display
        if identity not in result_ids or display is None or display.revision == 1:
            continue
        if not recovery_access.can_read(display.recovery):
            raise ResultBudgetExceeded(f"Cannot replace result {identity} with an unreadable recovery reference.")
        previous = result_store.snapshot_text(display.text)
        text = result_store.read_reference(display.recovery).decode("utf-8")
        shown = _display_item(item, text, display.recovery)
        # The emergency projection contains no body; only real status, source
        # completeness, references and checked recovery cursors survive.
        from dagent.harness_runtime.context import HeuristicTokenCounter

        projection = project_result(
            shown, 2**60, HeuristicTokenCounter(), read_available=True,
            max_body_chars=0, recovery_reference=display.recovery,
        )
        updated = display.model_copy(update={
            "text": projection.text, "displayed_spans": (), "revision": 1,
            "previous_display": previous,
        })
        updates[identity] = item.model_copy(update={"model_display": updated})
    return _replace_results(conversation, updates)
