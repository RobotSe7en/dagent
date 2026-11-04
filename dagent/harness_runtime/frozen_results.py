"""Choose model result displays once; undisplayed text goes to a file.

A display that shows a result completely has no file. A shortened display
points to the file that holds the full text: an overflow file written when the
result was received, or one written here when the display shortens an inline
result. These files are written best-effort and never opened again here.
"""

from __future__ import annotations

from typing import Any

from dagent.harness_runtime.result_projection import (
    ResultBudgetExceeded,
    ResultItem,
    TextCounter,
    file_readable_text,
    project_result,
)
from dagent.harness_runtime.result_storage import ResultStore
from dagent.schemas.context import ContextPolicy
from dagent.schemas.conversation import (
    ContentReference,
    ConversationState,
    FrozenResultDisplay,
    InlineContent,
    ToolResultMessage,
    UserMessage,
    stored_content_text,
)
from dagent.schemas.retention import ResultRetention


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


def _display_item(item: ResultItem, text: str, recovery: ContentReference) -> ResultItem:
    updates: dict[str, Any] = {"content": InlineContent(text=text), "model_display": None}
    if isinstance(item, ToolResultMessage):
        updates["artifacts"] = (recovery, *item.artifacts)
    else:
        updates["references"] = (recovery, *item.references)
    return item.model_copy(update=updates)


def _with_storage_warning(item: ResultItem, store: ResultStore, identity: str, exc: OSError) -> ResultItem:
    retention = item.retention or ResultRetention()
    warning = store.warning(identity, "content", exc)
    return item.model_copy(update={"retention": retention.model_copy(
        update={"storage_warnings": (*retention.storage_warnings, warning)},
    )})


def freeze_result_displays(
    conversation: ConversationState,
    *,
    result_store: ResultStore,
    policy: ContextPolicy,
    counter: TextCounter,
    files: bool,
    total_budget_tokens: int,
) -> ConversationState:
    """Water-fill a new result batch; existing displays are never redistributed.

    ``files`` is whether the model can read saved files. Without it, or when
    its text contains NUL, an inline result is never shortened. ``total_budget_tokens`` belongs only to displays
    without a receipt. The caller has already reserved fixed input, existing
    displays and model output.
    """

    pending = [(identity, item) for identity, item in _results(conversation) if item.model_display is None]
    if not pending:
        return conversation
    candidates = []
    for identity, item in pending:
        planned: ContentReference | None = None
        # Tokens of the complete inline display, which needs no file.
        complete_tokens: int | None = None
        if isinstance(item.content, ContentReference):
            # Already saved when received: show its stored preview. Whether
            # the model can open the file only changes the recovery notice.
            shown, shorten = item, files
        elif files and file_readable_text(item.content.text):
            # Text with NUL stays complete: read_file could not open its file.
            planned = result_store.text_reference(identity, item.content.text)
            shown, shorten = _display_item(item, item.content.text, planned), True
            complete_tokens = counter.count_text(project_result(item, 2**60, counter, read_available=False).text)
        else:
            shown, shorten = item, False
        minimum = project_result(
            shown, 2**60, counter, read_available=shorten,
            max_body_chars=0 if shorten else None, recovery_reference=planned,
        )
        floor = counter.count_text(minimum.text)
        if complete_tokens is not None and complete_tokens <= floor + policy.max_tool_result_tokens:
            wanted_tokens = complete_tokens
        else:
            wanted = project_result(
                shown, floor + policy.max_tool_result_tokens, counter,
                read_available=shorten, recovery_reference=planned,
            ) if shorten else minimum
            wanted_tokens = counter.count_text(wanted.text)
        # A complete tiny result can cost less than a reference-only display,
        # whose truncation notice is deliberately explicit.
        floor = min(floor, wanted_tokens)
        candidates.append((identity, item, shown, planned, shorten, complete_tokens, floor, wanted_tokens))

    floors = [candidate[6] for candidate in candidates]
    if sum(floors) > total_budget_tokens:
        raise ResultBudgetExceeded(
            f"Minimum information for {len(candidates)} new results requires {sum(floors)} tokens; "
            f"only {total_budget_tokens} are available.", minimum_tokens=sum(floors),
        )
    allocations = list(floors)
    remaining = total_budget_tokens - sum(allocations)
    active = [i for i, candidate in enumerate(candidates) if candidate[7] > allocations[i]]
    while remaining and active:
        share = max(1, remaining // len(active))
        for i in active:
            added = min(share, candidates[i][7] - allocations[i], remaining)
            allocations[i] += added
            remaining -= added
        active = [i for i in active if allocations[i] < candidates[i][7]]

    updates = {}
    for candidate, allocation in zip(candidates, allocations):
        identity, item, shown, planned, shorten, complete_tokens, _, _ = candidate
        if complete_tokens is not None and complete_tokens <= allocation:
            # The whole text fits, so no file is needed or mentioned.
            projection = project_result(item, 2**60, counter, read_available=False)
            display = FrozenResultDisplay(text=projection.text, displayed_spans=projection.displayed_spans)
            updates[identity] = item.model_copy(update={"model_display": display})
            continue
        projection = project_result(
            shown, allocation, counter, read_available=shorten, recovery_reference=planned,
        )
        if isinstance(item.content, ContentReference):
            # The preview is an excerpt with its own coordinates, so no
            # displayed source range can be stated for the saved file.
            display = FrozenResultDisplay(text=projection.text, recovery=item.content)
            updates[identity] = item.model_copy(update={"model_display": display})
            continue
        if planned is not None:
            try:
                saved = result_store.write_text(planned, item.content.text)
            except OSError as exc:
                failed = _with_storage_warning(item, result_store, identity, exc)
                projection = project_result(failed, allocation, counter, read_available=True)
                display = FrozenResultDisplay(text=projection.text, displayed_spans=projection.displayed_spans)
                updates[identity] = failed.model_copy(update={"model_display": display})
                continue
            display = FrozenResultDisplay(
                text=projection.text, recovery=saved, displayed_spans=projection.displayed_spans,
            )
            updates[identity] = item.model_copy(update={"content": saved, "model_display": display})
            continue
        # Without read_file the result is shown completely and has no file.
        display = FrozenResultDisplay(text=projection.text, displayed_spans=projection.displayed_spans)
        updates[identity] = item.model_copy(update={"model_display": display})
    return _replace_results(conversation, updates)


def reference_frozen_result_displays(
    conversation: ConversationState,
    *,
    result_store: ResultStore,
    files: bool,
    result_ids: set[str],
) -> ConversationState:
    """Make the explicit one-way emergency transition to reference-only displays.

    No saved file is read. The transition removes displayed text, so it is
    made only when the model can read files; otherwise every display stays as
    it is. A complete display without a file gets one, unless its text
    contains NUL, which read_file could not open.
    """

    from dagent.harness_runtime.context import HeuristicTokenCounter

    if not files:
        return conversation
    updates = {}
    for identity, item in _results(conversation):
        display = item.model_display
        if identity not in result_ids or display is None or display.revision == 1:
            continue
        recovery = display.recovery
        if recovery is None:
            if not file_readable_text(stored_content_text(item.content)):
                continue
            try:
                recovery = result_store.save_text(identity, stored_content_text(item.content))
            except OSError as exc:
                result_store.warning(identity, "content", exc)
                continue
            item = item.model_copy(update={"content": recovery})
        shown = _display_item(item, stored_content_text(item.content), recovery)
        # The emergency projection contains no body; only real status, source
        # completeness, references and recovery cursors survive.
        projection = project_result(
            shown, 2**60, HeuristicTokenCounter(), read_available=True,
            max_body_chars=0, recovery_reference=recovery,
        )
        updated = display.model_copy(update={
            "text": projection.text, "recovery": recovery, "displayed_spans": (), "revision": 1,
        })
        updates[identity] = item.model_copy(update={"model_display": updated})
    return _replace_results(conversation, updates)
