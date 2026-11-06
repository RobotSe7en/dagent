import shutil
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_RUNS = _REPO_ROOT / ".dagent" / "runs"


@pytest.fixture(autouse=True)
def cleanup_repo_default_run_workspaces():
    before = _run_workspace_names(_REPO_RUNS)
    yield
    after = _run_workspace_names(_REPO_RUNS)
    for name in after - before:
        shutil.rmtree(_REPO_RUNS / name, ignore_errors=True)


def _run_workspace_names(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {path.name for path in root.iterdir() if path.is_dir()}


@pytest.fixture(autouse=True)
def published_messages_are_never_changed_in_place(monkeypatch):
    """The publisher keeps published messages by reference, so the SDK must
    never change one in place. Fail any test where a message that was already
    published serializes differently at a later publication or at the end."""

    from dagent.harness_runtime.conversation_changes import ConversationChangePublisher

    original = ConversationChangePublisher.publish
    publishers = []

    def verify(publisher):
        for item, snapshot in publisher._published_snapshots.values():
            assert item.model_dump_json() == snapshot, (
                f"Published message {item.id} was changed in place after publication.")

    async def publish(self, *args, **kwargs):
        if not hasattr(self, "_published_snapshots"):
            self._published_snapshots = {}
            publishers.append(self)
        verify(self)
        await original(self, *args, **kwargs)
        if self.conversation is not None:
            for item in self.conversation.items:
                self._published_snapshots.setdefault(id(item), (item, item.model_dump_json()))

    monkeypatch.setattr(ConversationChangePublisher, "publish", publish)
    yield
    for publisher in publishers:
        verify(publisher)
