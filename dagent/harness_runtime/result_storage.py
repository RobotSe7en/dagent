"""Normalize large capability outputs into bounded workspace references."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from collections.abc import Callable, Iterable
import warnings

from dagent.schemas import CapabilityResult
from dagent.schemas.common import validate_runtime_directory
from dagent.schemas.context import ResultStoragePolicy
from dagent.schemas.conversation import (
    ContentReference,
    InlineContent,
    StoredContent,
    ConversationItem,
    ContextSummary,
    ResultObservation,
    ToolResultMessage,
    UserMessage,
    AssistantMessage,
)
from dagent.schemas.retention import ResultRetention, ResultStorageWarning
from dagent.schemas.common import Boundary
from dagent.capabilities.tools.boundary import enforce_path_allowed, BoundaryViolation
from dagent.harness_runtime.result_projection import result_references


class ResultStorageError(RuntimeError):
    """Required result data could not be persisted; execution already occurred."""

    def __init__(self, result: CapabilityResult, field: str, cause: OSError) -> None:
        self.result = result
        self.field = field
        retention = result.retention or ResultRetention()
        warning = ResultStorageWarning(
            field=field, error_type=type(cause).__name__, message=str(cause)
        )
        updates: dict[str, Any] = {}
        unavailable = retention.unavailable_fields
        if isinstance(result.value, (bytes, bytearray, memoryview)):
            updates["value"] = None
            unavailable = (*unavailable, "value")
        updates["retention"] = retention.model_copy(
            update={
                "storage_warnings": (*retention.storage_warnings, warning),
                "unavailable_fields": unavailable,
            }
        )
        self.audit_result = result.model_copy(update=updates)
        super().__init__(f"Cannot save required result {field}: {cause}")


class ResultStore:
    """Run-scoped filesystem owner used before, never during, projection."""

    def __init__(
        self,
        workspace_path: str | Path,
        runtime_directory: str,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        read_boundary: Boundary | None = None,
    ) -> None:
        self.workspace = Path(workspace_path).expanduser().resolve()
        runtime = PurePosixPath(validate_runtime_directory(runtime_directory))
        self.root = self.workspace.joinpath(*runtime.parts, "results").resolve()
        self.root.relative_to(self.workspace)
        self.on_event = on_event
        self.read_boundary = read_boundary

    def can_read(self, item: ToolResultMessage | ResultObservation) -> bool:
        paths = [reference.path for reference in result_references(item)]
        if item.retention and item.retention.continuation:
            paths.append(item.retention.continuation.path)
        try:
            for path in paths:
                target = (self.workspace / path).resolve()
                target.relative_to(self.workspace)
                if self.read_boundary is not None:
                    enforce_path_allowed(path, self.read_boundary, self.workspace)
                if not target.exists() or not os.access(target, os.R_OK):
                    return False
        except (BoundaryViolation, ValueError, OSError):
            return False
        return True

    def save_text(
        self, identity: str, text: str, *, field: str = "content"
    ) -> ContentReference:
        data = text.encode("utf-8")
        return _write_reference(
            self.workspace,
            self.root,
            _result_filename(identity, field, data, ".txt"),
            data,
            media_type="text/plain; charset=utf-8",
            preview=_head_tail_preview(text),
        )

    def warning(self, identity: str, field: str, exc: OSError) -> ResultStorageWarning:
        warning = ResultStorageWarning(
            field=field, error_type=type(exc).__name__, message=str(exc)
        )
        warnings.warn(
            f"Result storage warning ({identity}, {field}): {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
        if self.on_event:
            self.on_event(
                {
                    "type": "tool_result_storage_warning",
                    "invocation_id": identity,
                    "warning": warning.model_dump(mode="json"),
                }
            )
        return warning

    def ensure(
        self, item: ToolResultMessage | ResultObservation
    ) -> ToolResultMessage | ResultObservation:
        refs = result_references(item)
        reference_storage_failed = item.retention and any(
            warning.field == "references" for warning in item.retention.storage_warnings
        )
        if len(refs) > 4 and not reference_storage_failed:
            try:
                manifest = self.save_text(
                    item.id,
                    "\n".join(ref.model_dump_json() for ref in refs),
                    field="references",
                )
                field = (
                    "artifacts" if isinstance(item, ToolResultMessage) else "references"
                )
                item = item.model_copy(update={field: (manifest,)})
            except OSError as exc:
                warning = self.warning(item.id, "references", exc)
                retention = item.retention or ResultRetention()
                item = item.model_copy(
                    update={
                        "retention": retention.model_copy(
                            update={
                                "storage_warnings": (
                                    *retention.storage_warnings,
                                    warning,
                                )
                            }
                        )
                    }
                )
        if isinstance(item.content, ContentReference):
            return item
        retention = item.retention or ResultRetention()
        if retention.storage_warnings or retention.window_start is not None:
            return item
        try:
            reference = self.save_text(item.id, item.content.text)
        except OSError as exc:
            warning = self.warning(item.id, "content", exc)
            return item.model_copy(
                update={
                    "retention": retention.model_copy(
                        update={
                            "storage_warnings": (*retention.storage_warnings, warning)
                        }
                    )
                }
            )
        return item.model_copy(update={"content": reference})

    def archive(
        self, previous: ContextSummary | None, items: Iterable[ConversationItem]
    ) -> ContentReference | None:
        previous_ref = previous.result_manifest if previous else None
        archived_items = tuple(items)
        has_results = any(
            isinstance(item, ToolResultMessage)
            or (isinstance(item, UserMessage) and item.result_observations)
            for item in archived_items
        )
        if not has_results:
            return previous_ref

        def chunks() -> Iterable[bytes]:
            if previous_ref:
                target = (self.workspace / previous_ref.path).resolve()
                target.relative_to(self.workspace)
                digest = hashlib.sha256()
                length = 0
                with target.open("rb") as source:
                    for data in iter(lambda: source.read(65536), b""):
                        digest.update(data)
                        length += len(data)
                        yield data
                if (
                    length != previous_ref.byte_length
                    or digest.hexdigest() != previous_ref.sha256
                ):
                    raise OSError("Result manifest checksum mismatch")
            for item in archived_items:
                if isinstance(item, AssistantMessage) and item.tool_calls:
                    yield (
                        json.dumps(
                            {
                                "type": "tool_calls",
                                "calls": [
                                    call.model_dump(mode="json")
                                    for call in item.tool_calls
                                ],
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    ).encode("utf-8")
                results = (
                    (item,)
                    if isinstance(item, ToolResultMessage)
                    else (
                        item.result_observations
                        if isinstance(item, UserMessage)
                        else ()
                    )
                )
                for result in results:
                    saved = self.ensure(result)
                    yield (saved.model_dump_json() + "\n").encode("utf-8")

        try:
            return _write_chunks(
                self.workspace,
                self.root,
                None,
                chunks(),
                media_type="application/x-ndjson",
                preview="Earlier tool results and calls (JSONL)",
            )
        except OSError as exc:
            self.warning("compaction", "manifest", exc)
            raise


@dataclass(frozen=True)
class NormalizedCapabilityResult:
    """Bounded capability result plus typed externalization provenance."""

    result: CapabilityResult
    content: StoredContent
    references: tuple[ContentReference, ...]
    value_reference: ContentReference | None = None


def normalize_capability_result(
    result: CapabilityResult,
    *,
    workspace_path: str | Path,
    runtime_directory: str,
    policy: ResultStoragePolicy,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> NormalizedCapabilityResult:
    """Return a checkpoint-safe result plus model/audit content references."""

    workspace = Path(workspace_path).expanduser().resolve()
    runtime_path = PurePosixPath(validate_runtime_directory(runtime_directory))
    result_root = workspace.joinpath(*runtime_path.parts, "results").resolve()
    try:
        result_root.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("runtime_directory escapes the run workspace.") from exc
    references: list[ContentReference] = []
    content_reference: ContentReference | None = result.content_reference
    store = ResultStore(workspace, runtime_directory, on_event)
    retention = result.retention or ResultRetention()
    if on_event:
        for warning in retention.storage_warnings:
            on_event(
                {
                    "type": "tool_result_storage_warning",
                    "invocation_id": result.invocation_id,
                    "warning": warning.model_dump(mode="json"),
                }
            )

    display_content = _display_content(result)
    content_bytes = display_content.encode("utf-8")
    if (
        content_reference is None
        and len(content_bytes) > policy.max_inline_bytes
        and retention.window_start is None
    ):
        try:
            content_reference = store.save_text(result.invocation_id, display_content)
        except OSError as exc:
            warning = store.warning(result.invocation_id, "content", exc)
            retention = retention.model_copy(
                update={
                    "storage_warnings": (*retention.storage_warnings, warning),
                }
            )
    if content_reference is not None:
        references.append(content_reference)
        stored_content: StoredContent = content_reference
        normalized_content = content_reference.preview
    else:
        stored_content = InlineContent(text=display_content)
        normalized_content = display_content

    normalized_artifacts: list[dict[str, Any]] = []
    for index, artifact in enumerate(result.artifacts):
        try:
            normalized, reference = _normalize_artifact(
                workspace,
                result_root,
                result.invocation_id,
                index,
                artifact,
            )
        except OSError as exc:
            raise ResultStorageError(result, f"artifacts[{index}]", exc) from exc
        normalized_artifacts.append(normalized)
        if reference is not None:
            references.append(reference)

    normalized_value = result.value
    value_reference: ContentReference | None = None
    if result.value is None and content_reference is not None:
        normalized_value = content_reference.model_dump(mode="json")
        value_reference = content_reference
    value_media_type = "application/json"
    value_extension = ".json"
    if result.value is None:
        encoded_value = b""
    elif isinstance(result.value, (bytes, bytearray, memoryview)):
        encoded_value = bytes(result.value)
        value_media_type = "application/octet-stream"
        value_extension = ".bin"
    else:
        try:
            encoded_value = json.dumps(
                result.value,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise TypeError(
                "Capability result value must be JSON-serializable or bytes."
            ) from exc
    if result.value is not None and (
        isinstance(result.value, (bytes, bytearray, memoryview))
        or len(encoded_value) > policy.max_inline_bytes
    ):
        value_reference = _write_required_reference(
            result,
            "value",
            workspace,
            result_root,
            _result_filename(
                result.invocation_id,
                "value",
                encoded_value,
                value_extension,
            ),
            encoded_value,
            media_type=value_media_type,
            preview=(
                ""
                if value_media_type == "application/octet-stream"
                else _head_tail_preview(
                    encoded_value.decode("utf-8"),
                    limit=min(8192, max(256, policy.max_inline_bytes // 2)),
                )
            ),
        )
        references.append(value_reference)
        normalized_value = value_reference.model_dump(mode="json")
        normalized_artifacts.append(value_reference.model_dump(mode="json"))

    normalized_text_fields: dict[str, Any] = {}
    for field_name in ("stdout", "stderr", "error"):
        field_value = getattr(result, field_name)
        if not field_value:
            continue
        encoded = field_value.encode("utf-8")
        if len(encoded) <= policy.max_inline_bytes:
            continue
        try:
            reference = store.save_text(
                result.invocation_id, field_value, field=field_name
            )
        except OSError as exc:
            warning = store.warning(result.invocation_id, field_name, exc)
            retention = retention.model_copy(
                update={
                    "storage_warnings": (*retention.storage_warnings, warning),
                }
            )
            continue
        references.append(reference)
        normalized_text_fields[field_name] = reference.preview

    normalized_result = result.model_copy(
        update={
            "content": normalized_content,
            "content_reference": content_reference,
            "retention": retention,
            "value": normalized_value,
            "artifacts": normalized_artifacts,
            **normalized_text_fields,
        }
    )
    return NormalizedCapabilityResult(
        result=normalized_result,
        content=stored_content,
        references=tuple(references),
        value_reference=value_reference,
    )


def _display_content(result: CapabilityResult) -> str:
    if result.status == "completed":
        return result.content
    prefix = (
        "[BOUNDARY_VIOLATION]"
        if result.stop_reason == "BoundaryViolation"
        else "[TOOL_ERROR]"
    )
    return f"{prefix} {result.error or result.content}".rstrip()


def _normalize_artifact(
    workspace: Path,
    root: Path,
    invocation_id: str,
    index: int,
    artifact: dict[str, Any],
) -> tuple[dict[str, Any], ContentReference | None]:
    data = artifact.get("data")
    if data is None:
        if artifact.get("type") in {"image", "audio"}:
            raise ValueError("MCP image/audio result is missing base64 data.")
        return dict(artifact), None
    if not isinstance(data, str):
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    else:
        try:
            encoded = base64.b64decode(data, validate=True)
        except (ValueError, TypeError) as exc:
            if artifact.get("type") in {"image", "audio"}:
                raise ValueError(
                    "MCP image/audio result contains invalid base64 data."
                ) from exc
            encoded = data.encode("utf-8")
    media_type = str(artifact.get("mime_type") or "application/octet-stream")
    extension = mimetypes.guess_extension(media_type.split(";", 1)[0]) or ".bin"
    reference = _write_reference(
        workspace,
        root,
        _result_filename(
            invocation_id,
            f"artifact-{index}",
            encoded,
            extension,
        ),
        encoded,
        media_type=media_type,
        preview="",
    )
    normalized = {key: value for key, value in artifact.items() if key != "data"}
    normalized.update(reference.model_dump(mode="json"))
    return normalized, reference


def _result_filename(
    invocation_id: str,
    field_name: str,
    data: bytes,
    extension: str,
) -> str:
    invocation_digest = hashlib.sha256(invocation_id.encode("utf-8")).hexdigest()[:16]
    content_digest = hashlib.sha256(data).hexdigest()[:16]
    return f"{invocation_digest}-{field_name}-{content_digest}{extension}"


def _write_required_reference(
    result: CapabilityResult, field: str, *args: Any, **kwargs: Any
) -> ContentReference:
    try:
        return _write_reference(*args, **kwargs)
    except OSError as exc:
        raise ResultStorageError(result, field, exc) from exc


def _write_reference(
    workspace: Path,
    root: Path,
    filename: str,
    data: bytes,
    *,
    media_type: str,
    preview: str,
) -> ContentReference:
    return _write_chunks(
        workspace, root, filename, (data,), media_type=media_type, preview=preview
    )


def _write_chunks(
    workspace: Path,
    root: Path,
    filename: str | None,
    chunks: Iterable[bytes],
    *,
    media_type: str,
    preview: str,
) -> ContentReference:
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    length = 0
    descriptor, temporary_name = tempfile.mkstemp(prefix=".result-", dir=root)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            for data in chunks:
                handle.write(data)
                digest.update(data)
                length += len(data)
            handle.flush()
            os.fsync(handle.fileno())
        target = (root / (filename or f"manifest-{digest.hexdigest()}.jsonl")).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                "Capability result filename escapes result storage."
            ) from exc
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    relative = target.relative_to(workspace).as_posix()
    return ContentReference(
        path=relative,
        media_type=media_type,
        byte_length=length,
        sha256=digest.hexdigest(),
        preview=preview,
    )


def _head_tail_preview(text: str, *, limit: int = 8192) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return text[:head] + "\n...[EXTERNALIZED]...\n" + text[-tail:]


__all__ = ["NormalizedCapabilityResult", "normalize_capability_result"]
