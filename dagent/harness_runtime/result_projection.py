"""Pure rendering of runtime results with independent per-result limits."""

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

    def __init__(self, message: str, *, minimum_tokens: int | None = None) -> None:
        self.minimum_tokens = minimum_tokens
        super().__init__(message)


@dataclass(frozen=True)
class ResultProjection:
    text: str
    truncated: bool
    metadata_tokens: int
    body_tokens: int
    unrecoverable: bool
    displayed_spans: tuple[tuple[int, int], ...] = ()


def result_references(item: ResultItem) -> tuple[ContentReference, ...]:
    candidates = [item.content] if isinstance(item.content, ContentReference) else []
    if isinstance(item, ToolResultMessage):
        candidates.extend(item.artifacts)
        if item.value_reference is not None:
            candidates.append(item.value_reference)
    else:
        candidates.extend(item.references)
    if item.model_display is not None:
        candidates.append(item.model_display.recovery)
        if item.model_display.previous_display is not None:
            candidates.append(item.model_display.previous_display)
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
    max_body_chars: int | None = None,
    recovery_reference: ContentReference | None = None,
) -> ResultProjection:
    if item.model_display is not None:
        display = item.model_display
        marker = "[Recover received result with read_file: "
        start = display.text.find(marker)
        next_line = display.text.find("\n", start) if start >= 0 else -1
        body_start = (next_line + 1 if next_line >= 0 else len(display.text)) if start >= 0 else 0
        metadata = display.text[:body_start].rstrip("\n")
        total_tokens = counter.count_text(display.text)
        metadata_tokens = counter.count_text(metadata)
        return ResultProjection(
            display.text,
            display.revision == 1
            or hashlib.sha256(display.text[body_start:].encode("utf-8")).hexdigest() != display.recovery.sha256,
            metadata_tokens,
            max(0, total_tokens - metadata_tokens),
            not read_available,
            display.displayed_spans,
        )
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
        and retention.continuation.tool == "read_file"
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
        cursor_label = (
            "Continue source with"
            if recovery_reference is not None and cursor.tool == "read_file"
            else "Continue with"
        )
        cursor_text = f"[{cursor_label} {cursor.tool}: {json.dumps(args, ensure_ascii=False)}]"

    tail = item.capability_id == "tool.shell" or bool(retention and retention.exit_code is not None)

    def spans(chars: int) -> tuple[tuple[int, int], ...]:
        if chars <= 0:
            return ()
        if chars >= len(text) or file_window:
            return ((0, min(chars, len(text))),)
        if tail:
            return ((len(text) - chars, len(text)),)
        head = (chars + 1) // 2
        return ((0, head), (len(text) - (chars - head), len(text))) if chars > head else ((0, head),)

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
            metadata.append("[TRUNCATED] model display shortened; received result has undisplayed text")
        if file_window and retention:
            start = retention.window_start
            end = start + (retention.window_length if retention.window_length is not None else len(text))
            source_eof = {"complete": "true", "partial": "false", "unknown": "unknown"}[
                retention.source_completeness
            ]
            metadata.append(
                f"[File window: chars=[{start},{end}); source_eof={source_eof}; "
                "unit=Unicode code points, zero-based, end-exclusive]"
            )
            metadata.append(f"[Displayed: chars=[{start},{start + chars})]")
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
        if (recovery_reference is not None and retention and retention.continuation
                and retention.continuation.tool == "read_file"):
            # Undisplayed received text belongs to the immutable snapshot below.
            # Reading the mutable source again is only for not-yet-received data,
            # starting at the tool's actual continuation, never the display end.
            if retention.source_completeness != "complete":
                metadata.append(cursor_text)
        elif file_window and retention and retention.continuation:
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
        if recovery_reference is not None:
            shown = spans(chars)
            offset = shown[0][1] if shown and shown[0][0] == 0 else 0
            if text.startswith("\ufeff") and offset:
                offset -= 1  # read_file Unicode offsets exclude an initial BOM.
            args = {"path": recovery_reference.path, "offset_chars": offset, "limit_chars": 1024}
            metadata.append(f"[Recover received result with read_file: {json.dumps(args, ensure_ascii=False)}]")
        body = (
            text[:chars]
            if file_window
            else _excerpt(
                text,
                chars,
                tail=tail,
            )
        )
        meta = "\n".join(metadata)
        return "\n".join(part for part in (meta, body) if part), meta, body, unavailable

    initial_chars = len(text) if max_body_chars is None else min(len(text), max(0, max_body_chars))
    full, meta, body, unavailable = render(initial_chars)
    if counter.count_text(full) <= budget:
        metadata_tokens = counter.count_text(meta)
        return ResultProjection(
            full,
            externalized or initial_chars < len(text),
            metadata_tokens,
            counter.count_text(full) - metadata_tokens,
            unavailable,
            spans(initial_chars),
        )
    minimal, _, _, _ = render(0)
    if counter.count_text(minimal) > budget:
        raise ResultBudgetExceeded(
            f"Minimum information for result {item.id} exceeds {budget} tokens."
        )
    lo, hi = 0, initial_chars
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
        spans(lo),
    )


def project_results(
    items: Sequence[ResultItem],
    policy: ContextPolicy,
    counter: TextCounter,
    *,
    read_available: bool | Callable[[ResultItem], bool],
) -> dict[str, ResultProjection]:
    """Render independently without redistributing a shared result budget."""

    projections: dict[str, ResultProjection] = {}
    for item in items:
        readable = read_available(item) if callable(read_available) else read_available
        projections[item.id] = project_result(
            item,
            policy.max_tool_result_tokens,
            counter,
            read_available=readable,
        )
    return projections
