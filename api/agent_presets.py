"""Persistent agent presets for the local API/WebUI."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class _AgentPresetFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str = Field(min_length=1)
    description: str = ""
    max_steps: int = Field(default=8, ge=1)
    capability_ids: list[str] | None = None
    skills: list[str] | None = None


class AgentPreset(_AgentPresetFields):
    name: str = Field(min_length=1, max_length=64)


class AgentPresetRequest(AgentPreset):
    pass


class AgentPresetUpdateRequest(_AgentPresetFields):
    pass


class AgentPresetStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def list_names(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(path.stem for path in self.root.glob("*.json") if path.is_file())

    def list(self) -> list[AgentPreset]:
        return [self.load(name) for name in self.list_names()]

    def load(self, name: str) -> AgentPreset:
        clean_name = clean_agent_preset_name(name)
        path = self._path(clean_name)
        if not path.is_file():
            raise FileNotFoundError(f"Agent preset '{clean_name}' was not found.")
        preset = AgentPreset.model_validate_json(path.read_text(encoding="utf-8"))
        if preset.name != clean_name:
            raise ValueError(f"Agent preset file '{path.name}' contains name '{preset.name}'.")
        return preset

    def save(self, preset: AgentPreset) -> AgentPreset:
        clean_name = clean_agent_preset_name(preset.name)
        resolved = preset.model_copy(update={"name": clean_name}, deep=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(clean_name).write_text(
            json.dumps(resolved.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return resolved

    def delete(self, name: str) -> None:
        self._path(clean_agent_preset_name(name)).unlink(missing_ok=True)

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.json"


def clean_agent_preset_name(value: Any) -> str:
    name = str(value or "").strip()
    if not _AGENT_NAME_RE.fullmatch(name):
        raise ValueError("Agent preset names may contain only letters, numbers, underscores, and hyphens.")
    return name
