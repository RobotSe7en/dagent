"""Pure, minimum-information-first rendering of runtime results."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from typing import Protocol, Sequence
from collections.abc import Callable

from dagent.schemas.context import ContextPolicy
from dagent.schemas.conversation import (
    ContentReference,
    ResultObservation,
    ToolResultMessage,
    stored_content_text,
)


class TextCounter(Protocol):
    def count_text(self, text: str) -> int: ...


ResultItem = ToolResultMessage | ResultObservation


class ResultBudgetExceeded(ValueError):
    """Mandatory result information cannot fit the requested budget."""


@dataclass(frozen=True)
class ResultProjection:
    text: str
    truncated: bool
    metadata_tokens: int
    body_tokens: int
    unrecoverable: bool


def result_references(item: ResultItem) -> tuple[ContentReference, ...]:
    candidates = [item.content] if isinstance(item.content, ContentReference) else []
    if isinstance(item, ToolResultMessage):
        candidates.extend(item.artifacts)
        if item.value_reference is not None:
            candidates.append(item.value_reference)
    else:
        candidates.extend(item.references)
    unique = {(ref.path, ref.sha256): ref for ref in candidates}
    return tuple(unique.values())


def _excerpt(text: str, chars: int, *, tail: bool) -> str:
    if chars >= len(text):
        return text
    if chars <= 0:
        return ""
    if tail:
        return text[-chars:]
    head = (chars + 1) // 2
    return text[:head] + ("\n…\n" + text[-(chars - head) :] if chars > head else "")


def project_result(
    item: ResultItem,
    budget: int,
    counter: TextCounter,
    *,
    read_available: bool,
    reserve_excerpt: bool = False,
) -> ResultProjection:
    text = stored_content_text(item.content)
    refs = result_references(item)
    retention = item.retention
    if retention and retention.window_length is not None:
        text = text[: retention.window_length]
    source_partial = (
        retention is not None and retention.source_completeness == "partial"
    )
    externalized = (
        isinstance(item.content, ContentReference)
        and hashlib.sha256(text.encode("utf-8")).hexdigest() != item.content.sha256
    )
    status = f"[status={item.status}]"
    if isinstance(item, ResultObservation):
        status = f"Result {item.name}:\n" + status
    if isinstance(item, ResultObservation) and item.capability_id:
        status += f" [tool={item.capability_id}]"
        args = json.dumps(item.arguments, ensure_ascii=False)
        args_budget = min(32, max(1, budget // 8))
        while args and counter.count_text(args) > args_budget:
            args = args[: max(0, len(args) - max(1, len(args) // 4))]
        status += f" [args={args}{'…' if args != json.dumps(item.arguments, ensure_ascii=False) else ''}]"
    if retention and retention.exit_code is not None:
        status += f" [exit_code={retention.exit_code}]"
    if not text and not refs:
        status += " [EMPTY_RESULT]"
    if source_partial:
        status += " [SOURCE_TRUNCATED]"
    if retention and retention.storage_warnings:
        status += (
            " [storage warning: "
            + ", ".join(
                f"{warning.field}: {warning.error_type}"
                for warning in retention.storage_warnings
            )
            + "]"
        )
    reference_text = "\n".join(
        f"[Stored result: path={ref.path}; media_type={ref.media_type}; bytes={ref.byte_length}]"
        for ref in refs
    )
    cursor_text = ""
    file_window = bool(
        retention and retention.window_start is not None and retention.continuation
    )
    if retention and retention.continuation:
        cursor = retention.continuation
        args: dict[str, object] = {"path": cursor.path}
        if cursor.tool == "read_file":
            args.update(offset_chars=cursor.offset, limit_chars=cursor.limit)
        else:
            args.update(offset=cursor.offset, limit=cursor.limit)
            for name in ("pattern", "glob", "depth"):
                if getattr(cursor, name) is not None:
                    args[name] = getattr(cursor, name)
        cursor_text = (
            f"[Continue with {cursor.tool}: {json.dumps(args, ensure_ascii=False)}]"
        )

    def render(chars: int) -> tuple[str, str, str, bool]:
        truncated = chars < len(text) or externalized
        unavailable = (
            (truncated or source_partial) and not refs and not cursor_text
        ) or (
            (truncated or source_partial or bool(refs))
            and bool(refs or file_window)
            and not read_available
        )
        metadata = [status]
        if truncated:
            metadata.append("[TRUNCATED]")
        if unavailable:
            metadata.append(
                "[RECOVERY_UNAVAILABLE: "
                + (
                    "read_file unavailable, path denied or missing"
                    if refs or file_window
                    else "no saved original"
                )
                + "]"
            )
        if reference_text:
            metadata.append(reference_text)
        if file_window and retention and retention.continuation:
            if truncated or source_partial:
                args = {
                    "path": retention.continuation.path,
                    "offset_chars": retention.window_start + chars,
                    "limit_chars": min(1024, max(1, chars)),
                }
                metadata.append(
                    f"[Continue with read_file: {json.dumps(args, ensure_ascii=False)}]"
                )
        elif cursor_text:
            metadata.append(cursor_text)
        body = (
            text[:chars]
            if file_window
            else _excerpt(
                text,
                chars,
                tail=item.capability_id == "tool.shell"
                or bool(retention and retention.exit_code is not None),
            )
        )
        meta = "\n".join(metadata)
        return "\n".join(part for part in (meta, body) if part), meta, body, unavailable

    full, meta, body, unavailable = render(len(text))
    if reserve_excerpt:
        # Shortening can introduce recovery notices absent from the full result.
        minimum_metadata = render(0)[1]
        budget = min(
            budget,
            max(counter.count_text(meta), counter.count_text(minimum_metadata))
            + 64
            + 4,
        )
    if counter.count_text(full) <= budget:
        metadata_tokens = counter.count_text(meta)
        return ResultProjection(
            full,
            externalized,
            metadata_tokens,
            counter.count_text(full) - metadata_tokens,
            unavailable,
        )
    minimal, _, _, _ = render(0)
    if counter.count_text(minimal) > budget:
        raise ResultBudgetExceeded(
            f"Minimum information for result {item.id} exceeds {budget} tokens."
        )
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if counter.count_text(render(mid)[0]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    rendered, meta, body, unavailable = render(lo)
    metadata_tokens = counter.count_text(meta)
    return ResultProjection(
        rendered,
        True,
        metadata_tokens,
        counter.count_text(rendered) - metadata_tokens,
        unavailable,
    )


def project_results(
    items: Sequence[ResultItem],
    policy: ContextPolicy,
    counter: TextCounter,
    *,
    read_available: bool | Callable[[ResultItem], bool],
) -> dict[str, ResultProjection]:
    projections: dict[str, ResultProjection] = {}
    # A short excerpt is reserved in addition to the indivisible metadata.
    for item in items:
        readable = read_available(item) if callable(read_available) else read_available
        projections[item.id] = project_result(
            item,
            policy.max_tool_result_tokens,
            counter,
            read_available=readable,
            reserve_excerpt=True,
        )
    remaining = policy.max_total_tool_result_tokens - sum(
        counter.count_text(projections[item.id].text) for item in items
    )
    if remaining < 0:
        raise ResultBudgetExceeded(
            "Minimum tool result information exceeds the total tool result budget."
        )
    for item in reversed(items):
        readable = read_available(item) if callable(read_available) else read_available
        previous = counter.count_text(projections[item.id].text)
        budget = min(policy.max_tool_result_tokens, previous + remaining)
        projection = project_result(item, budget, counter, read_available=readable)
        remaining -= counter.count_text(projection.text) - previous
        projections[item.id] = projection
    return projections
