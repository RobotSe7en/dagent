"""Durable local resources carried by a conversation across run workspaces."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
from typing import Any

from dagent.schemas import (
    ContentReference,
    ConversationState,
    ToolResultMessage,
    UserMessage,
)
from dagent.schemas.common import validate_runtime_directory


class ConversationResourceError(RuntimeError):
    """Raised when retained conversation result data is unavailable or invalid."""


@dataclass(frozen=True)
class _IndexedResource:
    file_identity: tuple[int, ...]
    edges: tuple[tuple[ContentReference, str], ...]


class ConversationResourceIndex:
    """Reuse verified archive links without retaining historical file bodies.

    Each traversal still checks file identity, so deletion or replacement of an
    old leaf invalidates it immediately. Only changed or newly encountered files
    need another full read, digest check and schema parse. This index is runtime
    state, never a persisted assertion that a resource is trustworthy.
    """

    def __init__(self, workspace_path: str | Path) -> None:
        self.workspace = Path(workspace_path).expanduser().resolve()
        self._resources: dict[tuple[str, int, str, str], _IndexedResource] = {}

    def edges(
        self,
        reference: ContentReference,
        kind: str,
        read_reference: Callable[[ContentReference], bytes],
    ) -> tuple[tuple[ContentReference, str], ...]:
        target = _resolve_workspace_resource(self.workspace, reference.path)
        identity = _file_identity(target)
        key = (reference.path, reference.byte_length, reference.sha256, kind)
        cached = self._resources.get(key)
        if cached is not None and cached.file_identity == identity:
            return cached.edges
        data = read_reference(reference)
        edges = _resource_edges(data, kind)
        if identity != _file_identity(target):
            raise ConversationResourceError(f"Conversation resource changed during verification: {reference.path}.")
        self._resources[key] = _IndexedResource(identity, edges)
        return edges


class ConversationResourceStore:
    """Content-addressed store for externalized results, not mutable uploads."""

    def __init__(
        self,
        workspace_root: str | Path,
        runtime_directory: str,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve()
        self.runtime_directory = validate_runtime_directory(runtime_directory)
        runtime_path = PurePosixPath(self.runtime_directory)
        self.root = root.joinpath(*runtime_path.parts, "conversations").resolve()
        try:
            self.root.relative_to(root)
        except ValueError as exc:
            raise ValueError("runtime_directory escapes the runner workspace.") from exc
        # File identities this store verified, and the content-addressed links
        # of verified resources. Unchanged files are not reread at later run
        # boundaries; a changed one is verified again. Both caches are bounded
        # runtime state, never a persisted trust assertion.
        self._verified: dict[tuple[str, int, str], tuple[int, ...]] = {}
        self._edges: dict[tuple[str, str], tuple[tuple[ContentReference, str], ...]] = {}

    def _known(self, path: Path, byte_length: int, sha256: str) -> bool:
        key = (str(path), byte_length, sha256)
        try:
            identity = _file_identity(path)
        except OSError:
            return False
        if self._verified.get(key) != identity:
            return False
        _remember(self._verified, key, identity)
        return True

    def _read(self, path: Path, byte_length: int, sha256: str, *, label: str) -> bytes:
        try:
            before = _file_identity(path)
        except OSError as exc:
            raise ConversationResourceError(f"Conversation resource cannot be read: {label}.") from exc
        data = _read_verified(path, byte_length=byte_length, sha256=sha256, label=label)
        if _file_identity(path) == before:
            _remember(self._verified, (str(path), byte_length, sha256), before)
        return data

    def _note(self, path: Path, byte_length: int, sha256: str) -> None:
        _remember(self._verified, (str(path), byte_length, sha256), _file_identity(path))

    def persist(
        self,
        conversation: ConversationState,
        *,
        workspace_path: str | Path,
    ) -> None:
        """Copy retained result references into the stable object store."""

        workspace = Path(workspace_path).expanduser().resolve()
        def retain(reference: ContentReference) -> bytes:
            source = _resolve_workspace_resource(workspace, reference.path)
            data = self._read(source, reference.byte_length, reference.sha256, label=reference.path)
            self._write_object(conversation.id, reference, data)
            return data

        def known(reference: ContentReference) -> bool:
            return (self._known(_resolve_workspace_resource(workspace, reference.path),
                                reference.byte_length, reference.sha256)
                    and self._known(self._object_path(conversation.id, reference.sha256),
                                    reference.byte_length, reference.sha256))

        roots = ((ref, kind) for ref, _, kind in _resource_roots(conversation))
        for _ in iter_resource_closure(roots, retain, index=_VerifiedEdges(self, known)):
            pass

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

        # Frozen displays and compaction records contain exact relative paths.
        # Restore their declared closure without rewriting opaque source text.
        def restore(reference: ContentReference) -> bytes:
            destination = _resolve_workspace_resource(workspace, reference.path)
            if destination.is_file():
                return self._read(destination, reference.byte_length, reference.sha256, label=reference.path)
            data = self._read(self._object_path(conversation.id, reference.sha256),
                              reference.byte_length, reference.sha256, label=reference.path)
            _write_verified_file(destination, data)
            self._note(destination, reference.byte_length, reference.sha256)
            return data

        def known(reference: ContentReference) -> bool:
            return self._known(_resolve_workspace_resource(workspace, reference.path),
                               reference.byte_length, reference.sha256)

        roots = ((ref, kind) for ref, preserve, kind in _resource_roots(conversation) if preserve)
        for reference in iter_resource_closure(roots, restore, index=_VerifiedEdges(self, known)):
            materialized.setdefault(reference.sha256, reference.path)

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
            current = _resolve_workspace_resource(workspace, path)
            if current.is_file():
                if not self._known(current, byte_length, sha256):
                    self._read(current, byte_length, sha256, label=path)
                materialized[sha256] = path
                return path
            object_path = self._object_path(conversation.id, sha256)
            if not object_path.is_file():
                raise ConversationResourceError(
                    "Conversation resource is unavailable: "
                    f"{path} (sha256={sha256})."
                )
            data = self._read(object_path, byte_length, sha256, label=sha256)
            suffix = _safe_media_suffix(media_type)
            relative = (
                PurePosixPath(self.runtime_directory)
                / "history"
                / f"{sha256}{suffix}"
            ).as_posix()
            destination = _resolve_workspace_resource(workspace, relative)
            _write_verified_file(destination, data)
            self._note(destination, byte_length, sha256)
            materialized[sha256] = relative
            return relative

        items = []
        changed = False
        for item in conversation.items:
            if isinstance(item, ToolResultMessage) and item.model_display is None:
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
        reference: ContentReference,
        data: bytes,
    ) -> None:
        target = self._object_path(conversation_id, reference.sha256)
        if target.is_file():
            self._read(target, reference.byte_length, reference.sha256, label=reference.sha256)
            return
        _write_verified_file(target, data)
        self._note(target, reference.byte_length, reference.sha256)


class _VerifiedEdges:
    """Closure index over files a store verified: unchanged ones are not reread.

    Links depend only on content, so they are cached by digest; ``known``
    confirms that the file at the caller's location is still the verified one.
    """

    def __init__(self, store: ConversationResourceStore, known: Callable[[ContentReference], bool]) -> None:
        self.store = store
        self.known = known

    def edges(
        self,
        reference: ContentReference,
        kind: str,
        read_reference: Callable[[ContentReference], bytes],
    ) -> tuple[tuple[ContentReference, str], ...]:
        key = (reference.sha256, kind)
        cached = self.store._edges.get(key)
        if cached is not None and self.known(reference):
            return cached
        edges = _resource_edges(read_reference(reference), kind)
        _remember(self.store._edges, key, edges)
        return edges


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
    for resource, _, _ in _resource_roots(conversation):
        identity = (resource.path, resource.sha256)
        if identity in seen:
            continue
        seen.add(identity)
        yield resource.path, resource.byte_length, resource.sha256


def _resource_roots(
    conversation: ConversationState,
) -> Iterator[tuple[ContentReference, bool, str]]:
    for item in conversation.items:
        if isinstance(item, ToolResultMessage):
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
            for resource in resources:
                yield resource, item.model_display is not None, "opaque"
            if item.model_display is not None:
                yield item.model_display.recovery, True, "opaque"
                if item.model_display.previous_display is not None:
                    yield item.model_display.previous_display, True, "opaque"
        elif isinstance(item, UserMessage):
            for result in item.result_observations:
                if result.model_display is None:
                    continue
                if isinstance(result.content, ContentReference):
                    yield result.content, True, "opaque"
                for reference in result.references:
                    yield reference, True, "opaque"
                yield result.model_display.recovery, True, "opaque"
                if result.model_display.previous_display is not None:
                    yield result.model_display.previous_display, True, "opaque"
        else:
            continue
    if conversation.summary is not None and conversation.summary.compaction_record is not None:
        yield conversation.summary.compaction_record, True, "record"
        if conversation.summary.result_manifest is not None:
            yield conversation.summary.result_manifest, True, "legacy_results"
    if conversation.compaction_state is not None:
        for failure in conversation.compaction_state.failed_records:
            yield failure.record, True, "record"


def iter_resource_closure(
    roots: Iterable[tuple[ContentReference, str]],
    read_reference: Callable[[ContentReference], bytes],
    *,
    index: ConversationResourceIndex | _VerifiedEdges | None = None,
) -> Iterator[ContentReference]:
    """Read schema-owned recovery edges, keeping ordinary result data opaque."""

    pending = list(roots)
    seen: set[tuple[str, str, str]] = set()
    while pending:
        reference, kind = pending.pop()
        identity = (reference.path, reference.sha256, kind)
        if identity in seen:
            continue
        seen.add(identity)
        edges = (index.edges(reference, kind, read_reference) if index is not None
                 else _resource_edges(read_reference(reference), kind))
        yield reference
        pending.extend(edges)


def _resource_edges(data: bytes, kind: str) -> tuple[tuple[ContentReference, str], ...]:
    """Traverse only schema-owned links; ordinary result JSON stays opaque."""

    if kind == "opaque":
        return ()
    from dagent.harness_runtime.compaction import CompactionRecordV1, CompactionSourceV1

    try:
        if kind == "record":
            record = CompactionRecordV1.model_validate_json(data)
            edges = [(record.source, "source"), *((ref, "opaque") for ref in record.refs)]
            if record.previous_record is not None:
                edges.append((record.previous_record, "record"))
            if record.reused_from is not None:
                edges.append((record.reused_from, "record"))
            return tuple(edges)
        if kind == "source":
            source = CompactionSourceV1.model_validate_json(data)
            edges = [(ref, "opaque") for ref in source.refs]
            if source.previous_result_manifest is not None:
                edges.append((source.previous_result_manifest, "legacy_results"))
            return tuple(edges)
        if kind == "legacy_results":
            edges = []
            from dagent.harness_runtime.result_projection import result_references

            for line in data.splitlines():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("Result manifest entries must be objects.")
                if row.get("type") == "tool_result":
                    result = ToolResultMessage.model_validate(row)
                    edges.extend((ref, "opaque") for ref in result_references(result))
            return tuple(edges)
    except (ValueError, UnicodeError) as exc:
        raise ConversationResourceError(f"Invalid retained conversation {kind} resource.") from exc
    raise ValueError(f"Unsupported conversation resource kind: {kind}")


def _has_conversation_resources(conversation: ConversationState) -> bool:
    return next(_conversation_resource_records(conversation), None) is not None


def _file_identity(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


_CACHE_LIMIT = 65536


def _remember(cache: dict[Any, Any], key: Any, value: Any) -> None:
    # Least recently used entries go first, so a long-lived Runner whose runs
    # use fresh workspaces keeps bounded memory.
    cache.pop(key, None)
    cache[key] = value
    while len(cache) > _CACHE_LIMIT:
        del cache[next(iter(cache))]


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
