"""Load editable agent profiles from YAML files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class AgentProfile(BaseModel):
    name: str
    role: str
    description: str = ""
    system_prompt: str
    user_prompt: str

    def render_user_prompt(self, **values: Any) -> str:
        rendered = self.user_prompt
        for key, value in values.items():
            rendered = rendered.replace("{{ " + key + " }}", str(value))
            rendered = rendered.replace("{{" + key + "}}", str(value))
        return rendered


class ProfileStore:
    """Filesystem-backed profile store.

    Profiles are plain YAML files so a WebUI can edit them without changing
    Python code.
    """

    def __init__(self, directory: str | Path = "profiles") -> None:
        self.directory = Path(directory)

    def load(self, name: str) -> AgentProfile:
        path = self.directory / f"{name}.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Profile '{path}' must contain a YAML mapping.")
        return AgentProfile.model_validate(data)

