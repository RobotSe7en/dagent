"""Durable local resources carried by a conversation across run workspaces."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath

from dagent.schemas import (
    Attachment,
    ContentReference,
    ConversationState,
    ToolResultMessage,
    UserMessage,
)


_RESOURCE_DIRECTORY = ".dagent/conversations"
_MATERIALIZED_DIRECTORY = ".dagent/history"


class ConversationResourceError(RuntimeError):
    """Raised before model execution when retained conversation data is unavailable."""


class ConversationResourceStore:
    """Content-addressed local store for attachments and externalized results."""

    def __init__(self, workspace_root: str | Path) -> None:
        root = Path(workspace_root).expanduser().resolve()
        self.root = (root / _RESOURCE_DIRECTORY).resolve()

    def persist(
        self,
        conversation: ConversationState,
        *,
        workspace_path: str | Path,
    ) -> None:
        """Copy every typed conversation resource into the stable object store."""

        workspace = Path(workspace_path).expanduser().resolve()
        for path, byte_length, sha256 in _conversation_resource_records(conversation):
            source = _resolve_workspace_resource(workspace, path)
            data = _read_verified(
                source,
                byte_length=byte_length,
                sha256=sha256,
                label=path,
            )
            self._write_object(conversation.id, sha256, data)

    def materialize(
        self,
        conversation: ConversationState,
        *,
        workspace_path: str | Path,
    ) -> ConversationState:
        """Copy retained resources into a new run workspace and rebase typed paths."""

        if not _has_conversation_resources(conversation):
            return conversation
        workspace = Path(workspace_path).expanduser().resolve()
        materialized: dict[str, str] = {}

        def rebase(
            *,
            path: str,
            media_type: str,
            byte_length: int,
            sha256: str,
        ) -> str:
            existing = materialized.get(sha256)
            if existing is not None:
                return existing
            object_path = self._object_path(conversation.id, sha256)
            if not object_path.is_file():
                current = _resolve_workspace_resource(workspace, path)
                if current.is_file():
                    data = _read_verified(
                        current,
                        byte_length=byte_length,
                        sha256=sha256,
                        label=path,
                    )
                    self._write_object(conversation.id, sha256, data)
                else:
                    raise ConversationResourceError(
                        "Conversation resource is unavailable: "
                        f"{path} (sha256={sha256})."
                    )
            data = _read_verified(
                object_path,
                byte_length=byte_length,
                sha256=sha256,
                label=sha256,
            )
            suffix = _safe_media_suffix(media_type)
            relative = f"{_MATERIALIZED_DIRECTORY}/{sha256}{suffix}"
            destination = _resolve_workspace_resource(workspace, relative)
            _write_verified_file(destination, data)
            materialized[sha256] = relative
            return relative

        items = []
        changed = False
        for item in conversation.items:
            if isinstance(item, UserMessage):
                attachments = tuple(
                    attachment.model_copy(
                        update={
                            "path": rebase(
                                path=attachment.path,
                                media_type=attachment.media_type,
                                byte_length=attachment.byte_length,
                                sha256=attachment.sha256,
                            )
                        }
                    )
                    for attachment in item.attachments
                )
                updated = item.model_copy(update={"attachments": attachments})
            elif isinstance(item, ToolResultMessage):
                content = item.content
                if isinstance(content, ContentReference):
                    content = _rebase_reference(content, rebase)
                value_reference = item.value_reference
                value = item.value
                if value_reference is not None:
                    value_reference = _rebase_reference(value_reference, rebase)
                    value = value_reference.model_dump(mode="json")
                artifacts = tuple(
                    _rebase_reference(reference, rebase)
                    for reference in item.artifacts
                )
                updated = item.model_copy(
                    update={
                        "content": content,
                        "value": value,
                        "value_reference": value_reference,
                        "artifacts": artifacts,
                    }
                )
            else:
                updated = item
            changed = changed or updated != item
            items.append(updated)

        if not changed:
            return conversation
        return conversation.model_copy(
            update={
                "revision": conversation.revision + 1,
                "items": tuple(items),
            }
        )

    def _object_path(self, conversation_id: str, sha256: str) -> Path:
        conversation_key = hashlib.sha256(
            conversation_id.encode("utf-8")
        ).hexdigest()
        target = (
            self.root / conversation_key / sha256[:2] / sha256
        ).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ConversationResourceError(
                "Conversation resource object path escapes its store."
            ) from exc
        return target

    def _write_object(
        self,
        conversation_id: str,
        sha256: str,
        data: bytes,
    ) -> None:
        target = self._object_path(conversation_id, sha256)
        if target.is_file():
            _read_verified(
                target,
                byte_length=len(data),
                sha256=sha256,
                label=sha256,
            )
            return
        _write_verified_file(target, data)


def _rebase_reference(
    reference: ContentReference,
    rebase: Callable[..., str],
) -> ContentReference:
    path = rebase(
        path=reference.path,
        media_type=reference.media_type,
        byte_length=reference.byte_length,
        sha256=reference.sha256,
    )
    return reference.model_copy(update={"path": path})


def _conversation_resource_records(
    conversation: ConversationState,
) -> Iterator[tuple[str, int, str]]:
    seen: set[tuple[str, str]] = set()
    for item in conversation.items:
        if isinstance(item, UserMessage):
            resources: tuple[Attachment | ContentReference, ...] = item.attachments
        elif isinstance(item, ToolResultMessage):
            content = (
                (item.content,)
                if isinstance(item.content, ContentReference)
                else ()
            )
            value = (
                (item.value_reference,)
                if item.value_reference is not None
                else ()
            )
            resources = (*content, *value, *item.artifacts)
        else:
            continue
        for resource in resources:
            identity = (resource.path, resource.sha256)
            if identity in seen:
                continue
            seen.add(identity)
            yield (
                resource.path,
                resource.byte_length,
                resource.sha256,
            )


def _has_conversation_resources(conversation: ConversationState) -> bool:
    return next(_conversation_resource_records(conversation), None) is not None


def _resolve_workspace_resource(workspace: Path, relative: str) -> Path:
    target = (workspace / PurePosixPath(relative)).resolve()
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise ConversationResourceError(
            f"Conversation resource escapes its workspace: {relative}."
        ) from exc
    return target


def _read_verified(
    path: Path,
    *,
    byte_length: int,
    sha256: str,
    label: str,
) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConversationResourceError(
            f"Conversation resource cannot be read: {label}."
        ) from exc
    if len(data) != byte_length:
        raise ConversationResourceError(
            f"Conversation resource size mismatch: {label}."
        )
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ConversationResourceError(
            f"Conversation resource SHA-256 mismatch: {label}."
        )
    return data


def _write_verified_file(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        if target.read_bytes() == data:
            return
        raise ConversationResourceError(
            f"Conversation resource destination conflicts with existing data: {target}."
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".conversation-resource-",
        dir=target.parent,
    )
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


def _safe_media_suffix(media_type: str) -> str:
    guessed = mimetypes.guess_extension(media_type.split(";", 1)[0]) or ""
    if (
        guessed
        and len(guessed) <= 12
        and guessed.startswith(".")
        and guessed[1:].isalnum()
    ):
        return guessed.lower()
    return ""


__all__ = ["ConversationResourceError", "ConversationResourceStore"]
