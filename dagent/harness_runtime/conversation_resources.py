"""Compaction archives carried by a conversation across run workspaces.

Only the compaction-archive chain (records, sources and legacy result
manifests) is persisted and restored. Tool-output files are never copied,
restored or rewritten; their paths stay as they were written.
"""

from __future__ import annotations

from dagent.harness_runtime.conversation_changes import note_resource

import hashlib
import os
import tempfile
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

from dagent.schemas import (
    ContentReference,
    ConversationState,
)
from dagent.schemas.common import validate_runtime_directory
from dagent.capabilities.tools.runtime_paths import RuntimePaths


class ConversationResourceError(RuntimeError):
    """Raised when a retained compaction archive is unavailable or invalid."""


class ConversationResourceStore:
    """Content-addressed store for compaction archives, not tool output."""

    def __init__(
        self,
        workspace_root: str | Path,
        runtime_directory: str,
    ) -> None:
        root = Path(workspace_root).expanduser().resolve()
        self.runtime_directory = validate_runtime_directory(runtime_directory)
        self.paths = RuntimePaths(root, self.runtime_directory)
        try:
            self.root = (self.paths.base / "conversations").resolve()
            self.root.relative_to(self.paths.base if self.paths.external else root)
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
        """Copy the compaction-archive chain into the stable object store."""

        workspace = Path(workspace_path).expanduser().resolve()
        def retain(reference: ContentReference) -> bytes:
            source = _resolve_workspace_resource(workspace, reference.path, self.runtime_directory)
            data = self._read(source, reference.byte_length, reference.sha256, label=reference.path)
            self._write_object(conversation.id, reference, data)
            return data

        def known(reference: ContentReference) -> bool:
            return (self._known(_resolve_workspace_resource(workspace, reference.path, self.runtime_directory),
                                reference.byte_length, reference.sha256)
                    and self._known(self._object_path(conversation.id, reference.sha256),
                                    reference.byte_length, reference.sha256))

        for _ in iter_resource_closure(_resource_roots(conversation), retain, index=_VerifiedEdges(self, known)):
            pass

    def materialize(
        self,
        conversation: ConversationState,
        *,
        workspace_path: str | Path,
    ) -> ConversationState:
        """Restore the compaction-archive chain into a run workspace.

        Archives keep their exact relative paths, so nothing is rewritten and
        the conversation is returned unchanged. Tool-output files are left as
        they are; a missing one is simply missing.
        """

        roots = tuple(_resource_roots(conversation))
        if not roots:
            return conversation
        workspace = Path(workspace_path).expanduser().resolve()

        def restore(reference: ContentReference) -> bytes:
            destination = _resolve_workspace_resource(workspace, reference.path, self.runtime_directory)
            if destination.is_file():
                return self._read(destination, reference.byte_length, reference.sha256, label=reference.path)
            data = self._read(self._object_path(conversation.id, reference.sha256),
                              reference.byte_length, reference.sha256, label=reference.path)
            _write_verified_file(destination, data)
            self._note(destination, reference.byte_length, reference.sha256)
            note_resource(reference, workspace, self.runtime_directory)
            return data

        def known(reference: ContentReference) -> bool:
            return self._known(_resolve_workspace_resource(workspace, reference.path, self.runtime_directory),
                               reference.byte_length, reference.sha256)

        for _ in iter_resource_closure(roots, restore, index=_VerifiedEdges(self, known)):
            pass
        for reference, _ in roots:
            note_resource(reference, workspace, self.runtime_directory)
        return conversation

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


def _resource_roots(
    conversation: ConversationState,
) -> Iterator[tuple[ContentReference, str]]:
    """The compaction archives a conversation links to; never tool output."""

    if conversation.summary is not None and conversation.summary.compaction_record is not None:
        yield conversation.summary.compaction_record, "record"
        if conversation.summary.result_manifest is not None:
            yield conversation.summary.result_manifest, "legacy_results"
    if conversation.compaction_state is not None:
        for failure in conversation.compaction_state.failed_records:
            yield failure.record, "record"


def iter_resource_closure(
    roots: Iterable[tuple[ContentReference, str]],
    read_reference: Callable[[ContentReference], bytes],
    *,
    index: _VerifiedEdges | None = None,
) -> Iterator[ContentReference]:
    """Follow the links between compaction archives."""

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
    """Links from one archive to other archives; tool-output references are not followed."""

    if kind == "legacy_results":
        # A result manifest is itself archived; the tool output it lists is not.
        return ()
    from dagent.harness_runtime.compaction import CompactionRecordV1, CompactionSourceV1

    try:
        if kind == "record":
            record = CompactionRecordV1.model_validate_json(data)
            edges = [(record.source, "source")]
            if record.previous_record is not None:
                edges.append((record.previous_record, "record"))
            if record.reused_from is not None:
                edges.append((record.reused_from, "record"))
            return tuple(edges)
        if kind == "source":
            source = CompactionSourceV1.model_validate_json(data)
            if source.previous_result_manifest is not None:
                return ((source.previous_result_manifest, "legacy_results"),)
            return ()
    except (ValueError, UnicodeError) as exc:
        raise ConversationResourceError(f"Invalid retained conversation {kind} resource.") from exc
    raise ValueError(f"Unsupported conversation resource kind: {kind}")


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


def _resolve_workspace_resource(
    workspace: Path, relative: str, runtime_directory: str | None = None,
) -> Path:
    try:
        return RuntimePaths.current(workspace, runtime_directory).resolve(relative)
    except ValueError as exc:
        raise ConversationResourceError(
            f"Conversation resource escapes its workspace: {relative}."
        ) from exc


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


__all__ = ["ConversationResourceError", "ConversationResourceStore"]
