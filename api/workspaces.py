from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname


class LocalWorkspaceStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def project_workspace_uri(self, project_id: str) -> str:
        return (self.root / project_id / "workspace").as_uri()

    def conversation_workspace_uri(self, conversation_id: str, *, project_id: str | None = None) -> str:
        if project_id is None:
            return (self.root / "_conversations" / conversation_id / "workspace").as_uri()
        return self.project_workspace_uri(project_id)

    def local_path_for(self, uri: str) -> Path:
        path = self._file_uri_path(uri)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def sync_in(self, uri: str) -> Path:
        return self.local_path_for(uri)

    def sync_out(self, uri: str, local: str | Path) -> None:
        self.local_path_for(uri)

    def open_file(self, uri: str, rel: str, mode: str = "rb"):
        workspace = self.local_path_for(uri)
        return self._resolve_relative_file(workspace, rel).open(mode)

    def _file_uri_path(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise ValueError(f"Unsupported workspace URI: {uri}")
        if parsed.netloc:
            raise ValueError(f"File workspace URI must not include a host: {uri}")
        path = Path(url2pathname(parsed.path)).expanduser().resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"File workspace URI must be under workspace root: {uri}") from exc
        return path

    def _resolve_relative_file(self, workspace: Path, rel: str) -> Path:
        relative = str(rel).strip().replace("\\", "/")
        if not relative:
            raise ValueError("Workspace file path is required.")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("Workspace file path must be a safe relative path.")
        resolved = (workspace / relative_path).resolve()
        try:
            resolved.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("Workspace file path must be a safe relative path.") from exc
        return resolved
