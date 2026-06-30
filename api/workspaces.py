from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse


class LocalWorkspaceStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def project_workspace_uri(self, project_id: str) -> str:
        return f"file://{self.root / project_id / 'workspace'}"

    def local_path_for(self, uri: str) -> Path:
        path = self._file_uri_path(uri)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def sync_in(self, uri: str) -> Path:
        return self.local_path_for(uri)

    def sync_out(self, uri: str, local: str | Path) -> None:
        self.local_path_for(uri)

    def open_file(self, uri: str, rel: str, mode: str = "rb"):
        return (self.local_path_for(uri) / rel).open(mode)

    def _file_uri_path(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise ValueError(f"Unsupported workspace URI: {uri}")
        if parsed.netloc:
            raise ValueError(f"File workspace URI must not include a host: {uri}")
        return Path(unquote(parsed.path)).expanduser().resolve()
