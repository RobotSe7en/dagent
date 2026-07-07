"""Composition seam tests for `create_app` and `ServerExtensions`."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from api.app import create_app, state
from api.extensions import LOCAL_PRINCIPAL, DefaultExtensions, Principal
from api.storage import SQLiteStore
from api.workspaces import LocalWorkspaceStore


class RecordingExtensions(DefaultExtensions):
    def __init__(self) -> None:
        self.configured_app: FastAPI | None = None

    def configure_app(self, app: FastAPI) -> None:
        self.configured_app = app
        extra = APIRouter()

        @extra.get("/enterprise/ping")
        async def ping() -> dict[str, str]:
            return {"status": "enterprise"}

        app.include_router(extra)

    def principal(self, request) -> Principal:
        return Principal(user_id="u1", username="alice", display_name="Alice", role="member")


@pytest.fixture(autouse=True)
def restore_default_extensions():
    yield
    create_app()


def test_create_app_defaults_keep_single_user_behavior() -> None:
    app = create_app()
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    assert type(state.extensions) is DefaultExtensions
    principal = state.principal(None)
    assert principal == LOCAL_PRINCIPAL
    assert principal.is_admin


def test_create_app_applies_extensions() -> None:
    extensions = RecordingExtensions()
    app = create_app(extensions)
    assert extensions.configured_app is app
    client = TestClient(app)
    assert client.get("/enterprise/ping").json() == {"status": "enterprise"}
    assert client.get("/health").json() == {"status": "ok"}
    principal = state.principal(None)
    assert principal.username == "alice"
    assert not principal.is_admin


def test_default_factories_build_local_backends(tmp_path: Path) -> None:
    extensions = DefaultExtensions()
    store = extensions.store_factory(tmp_path)
    try:
        assert isinstance(store, SQLiteStore)
        assert (tmp_path / "api.sqlite3").exists()
    finally:
        store.close()
    workspaces = extensions.workspace_factory(tmp_path)
    assert isinstance(workspaces, LocalWorkspaceStore)
    assert workspaces.root == (tmp_path / "projects").resolve()
