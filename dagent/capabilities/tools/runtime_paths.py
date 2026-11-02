"""Physical runtime storage behind workspace-relative result references.

This module is stdlib-only because shell output collection also runs in the
sandbox worker. No links or placeholder files are created in the workspace.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath


_RUNTIME_DIRECTORY: ContextVar[str] = ContextVar(
    "dagent_runtime_directory", default=".runtime"
)


@contextmanager
def runtime_storage_context(directory: str) -> Iterator[None]:
    token = _RUNTIME_DIRECTORY.set(directory)
    try:
        yield
    finally:
        _RUNTIME_DIRECTORY.reset(token)


@dataclass(frozen=True)
class RuntimePaths:
    workspace: Path
    directory: str

    @classmethod
    def current(cls, workspace: str | Path, directory: str | None = None) -> RuntimePaths:
        return cls(Path(workspace).resolve(), directory or _RUNTIME_DIRECTORY.get())

    @property
    def external(self) -> bool:
        return Path(self.directory).is_absolute()

    @property
    def logical_directory(self) -> str:
        return ".runtime" if self.external else self.directory

    @property
    def logical_root(self) -> Path:
        return self.workspace.joinpath(*PurePosixPath(self.logical_directory).parts)

    @property
    def base(self) -> Path:
        if self.external:
            return Path(self.directory).resolve()
        return self.logical_root.resolve()

    @property
    def root(self) -> Path:
        if not self.external:
            return self.base
        key = hashlib.sha256(str(self.workspace).encode("utf-8")).hexdigest()
        target = (self.base / "runs" / key).resolve()
        target.relative_to(self.base)
        return target

    @property
    def boundary(self) -> Path:
        # Relative storage retains the released workspace-wide containment rule.
        return self.root if self.external else self.workspace

    def resolve(self, path: str | Path) -> Path:
        # The external namespace is virtual: workspace symlinks must not
        # redirect it before it is mapped to physical storage.
        logical = Path(os.path.abspath(self.workspace / path))
        if self.is_runtime_path(logical):
            relative = logical.relative_to(self.logical_root)
            target = (self.root / relative).resolve()
            target.relative_to(self.root)
            return target
        target = (self.workspace / path).resolve()
        target.relative_to(self.workspace)
        return target

    def relative(self, path: Path) -> str:
        if self.external:
            try:
                relative = path.relative_to(self.root)
            except ValueError:
                pass
            else:
                return (PurePosixPath(self.logical_directory) / relative.as_posix()).as_posix()
        return path.relative_to(self.workspace).as_posix()

    def is_runtime_path(self, path: str | Path) -> bool:
        if not self.external:
            return False
        try:
            Path(os.path.abspath(self.workspace / path)).relative_to(self.logical_root)
        except ValueError:
            return False
        return True
