"""Normalize large capability outputs into bounded workspace references."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from dagent.schemas import CapabilityResult
from dagent.schemas.context import ResultStoragePolicy
from dagent.schemas.conversation import ContentReference, InlineContent, StoredContent


def normalize_capability_result(
    result: CapabilityResult,
    *,
    workspace_path: str | Path,
    policy: ResultStoragePolicy,
) -> tuple[CapabilityResult, StoredContent, tuple[ContentReference, ...]]:
    """Return a checkpoint-safe result plus model/audit content references."""

    workspace = Path(workspace_path).expanduser().resolve()
    result_root = _safe_result_root(workspace, policy.internal_directory)
    references: list[ContentReference] = []
    content_reference: ContentReference | None = None

    display_content = _display_content(result)
    content_bytes = display_content.encode("utf-8")
    if len(content_bytes) > policy.max_inline_bytes:
        content_reference = _write_reference(
            workspace,
            result_root,
            _result_filename(
                result.invocation_id,
                "content",
                content_bytes,
                ".txt",
            ),
            content_bytes,
            media_type="text/plain; charset=utf-8",
            preview=_head_tail_preview(
                display_content,
                limit=min(8192, max(256, policy.max_inline_bytes // 2)),
            ),
        )
        references.append(content_reference)
        stored_content: StoredContent = content_reference
        normalized_content = content_reference.preview
    else:
        stored_content = InlineContent(text=display_content)
        normalized_content = display_content

    normalized_artifacts: list[dict[str, Any]] = []
    for index, artifact in enumerate(result.artifacts):
        normalized, reference = _normalize_artifact(
            workspace,
            result_root,
            result.invocation_id,
            index,
            artifact,
        )
        normalized_artifacts.append(normalized)
        if reference is not None:
            references.append(reference)

    normalized_value = result.value
    if result.value is None and content_reference is not None:
        normalized_value = content_reference.model_dump(mode="json")
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
    if (
        result.value is not None
        and (
            isinstance(result.value, (bytes, bytearray, memoryview))
        or len(encoded_value) > policy.max_inline_bytes
        )
    ):
        value_reference = _write_reference(
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
        reference = _write_reference(
            workspace,
            result_root,
            _result_filename(
                result.invocation_id,
                field_name,
                encoded,
                ".txt",
            ),
            encoded,
            media_type="text/plain; charset=utf-8",
            preview=_head_tail_preview(
                field_value,
                limit=min(8192, max(256, policy.max_inline_bytes // 2)),
            ),
        )
        references.append(reference)
        normalized_text_fields[field_name] = reference.preview

    normalized_result = result.model_copy(
        update={
            "content": normalized_content,
            "value": normalized_value,
            "artifacts": normalized_artifacts,
            **normalized_text_fields,
        }
    )
    return normalized_result, stored_content, tuple(references)


def _display_content(result: CapabilityResult) -> str:
    if result.status == "completed":
        return result.content
    prefix = (
        "[BOUNDARY_VIOLATION]"
        if result.stop_reason == "BoundaryViolation"
        else "[TOOL_ERROR]"
    )
    return f"{prefix} {result.error or result.content}".rstrip()


def _safe_result_root(workspace: Path, relative: str) -> Path:
    posix = PurePosixPath(relative)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
        raise ValueError("Result storage directory is unsafe.")
    root = workspace.joinpath(*posix.parts).resolve()
    try:
        root.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("Result storage directory escapes the run workspace.") from exc
    root.mkdir(parents=True, exist_ok=True)
    return root


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
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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
    normalized = {
        key: value
        for key, value in artifact.items()
        if key != "data"
    }
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


def _write_reference(
    workspace: Path,
    root: Path,
    filename: str,
    data: bytes,
    *,
    media_type: str,
    preview: str,
) -> ContentReference:
    target = (root / filename).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("Capability result filename escapes result storage.") from exc
    descriptor, temporary_name = tempfile.mkstemp(prefix=".result-", dir=root)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
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
        byte_length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        preview=preview,
    )


def _head_tail_preview(text: str, *, limit: int = 8192) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return text[:head] + "\n...[EXTERNALIZED]...\n" + text[-tail:]


__all__ = ["normalize_capability_result"]
