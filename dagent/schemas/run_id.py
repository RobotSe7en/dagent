"""Run id validation shared by runtime contracts and workspace creation."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath


def validate_run_id(run_id: str) -> None:
    if not run_id or run_id in {".", ".."}:
        raise ValueError("run_id must be a single directory name.")
    path = Path(run_id)
    windows_path = PureWindowsPath(run_id)
    if (
        path.is_absolute()
        or windows_path.is_absolute()
        or path.name != run_id
        or windows_path.name != run_id
    ):
        raise ValueError("run_id must be a single directory name.")
