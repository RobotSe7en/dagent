"""Server composition seams for the API backend.

The open-source server is single-user: one implicit local principal with the
admin role, SQLite storage, and local workspaces. A separate distribution can
pass a `ServerExtensions` implementation to `create_app` to supply identity,
storage, workspace, and app-level wiring without patching this package.

One process hosts one composed app: `create_app` configures the module-level
`ApiState`, so calling it again reconfigures shared state rather than
producing an isolated instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

    from api.storage import Store
    from api.workspaces import LocalWorkspaceStore


@dataclass(frozen=True)
class Principal:
    """Identity attached to a request.

    The single-user server always resolves `LOCAL_PRINCIPAL`; multi-user
    distributions resolve one principal per authenticated session.
    """

    user_id: str
    username: str
    display_name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


LOCAL_PRINCIPAL = Principal(
    user_id="default",
    username="default",
    display_name="Local user",
    role="admin",
)


@runtime_checkable
class ServerExtensions(Protocol):
    """Composition seams consumed by `create_app` and `ApiState`."""

    def configure_app(self, app: FastAPI) -> None:
        """Add middleware, routers, and handlers after core routes mount."""

    def principal(self, request: Request) -> Principal:
        """Resolve the identity for a request; raise `HTTPException` to reject."""

    def store_factory(self, data_dir: Path) -> Store:
        """Create the persistence store rooted at the API data directory."""

    def workspace_factory(self, data_dir: Path) -> LocalWorkspaceStore:
        """Create the workspace store rooted at the API data directory."""


class DefaultExtensions:
    """Single-user defaults: local admin principal, SQLite, local workspaces."""

    def configure_app(self, app: FastAPI) -> None:
        return None

    def principal(self, request: Request) -> Principal:
        return LOCAL_PRINCIPAL

    def store_factory(self, data_dir: Path) -> Store:
        from api.storage import SQLiteStore

        return SQLiteStore(data_dir / "api.sqlite3")

    def workspace_factory(self, data_dir: Path) -> LocalWorkspaceStore:
        from api.workspaces import LocalWorkspaceStore

        return LocalWorkspaceStore(data_dir / "projects")
