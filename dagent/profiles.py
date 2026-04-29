"""Load editable agent profiles from YAML files."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class AgentProfile(BaseModel):
    name: str
    role: str
    description: str = ""
    layers: list[str] = Field(default_factory=list)
    layer_contents: dict[str, str] = Field(default_factory=dict)
    memory_file: str | None = None
    memory: str = ""
    output_format: str = "text"

    def render_layers(self) -> list[str]:
        return [
            self.layer_contents[layer].strip()
            for layer in self.layers
            if self.layer_contents.get(layer, "").strip()
        ]


class ProfileStore:
    """Filesystem-backed profile store.

    Profiles are plain YAML files so a WebUI can edit them without changing
    Python code.
    """

    def __init__(self, directory: str | Path = "profiles") -> None:
        self.directory = Path(directory)

    def load(self, name: str) -> AgentProfile:
        profile_dir = self.directory / name
        path = profile_dir / "profile.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Profile '{path}' must contain a YAML mapping.")
        layer_contents = {}
        for layer in data.get("layers", []):
            layer_path = profile_dir / layer
            if layer_path.exists():
                layer_contents[layer] = layer_path.read_text(encoding="utf-8")
        memory_file = data.get("memory_file")
        memory = ""
        if memory_file:
            memory_path = profile_dir / str(memory_file)
            if memory_path.exists():
                memory = memory_path.read_text(encoding="utf-8")
        data["layer_contents"] = layer_contents
        data["memory"] = memory
        return AgentProfile.model_validate(data)
