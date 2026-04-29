"""Global YAML configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator


class ProviderConfig(BaseModel):
    base_url: str
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    timeout_seconds: float = 60
    strip_thinking: bool = True

    @model_validator(mode="after")
    def resolve_api_key(self) -> "ProviderConfig":
        if self.api_key:
            return self
        if self.api_key_env:
            env_value = os.environ.get(self.api_key_env)
            if env_value:
                self.api_key = env_value
                return self
        self.api_key = "not-needed"
        return self


class DagentConfig(BaseModel):
    provider: ProviderConfig
    profiles: "ProfilesConfig" = Field(default_factory=lambda: ProfilesConfig())


class ProfilesConfig(BaseModel):
    directory: str = "profiles"
    conversation: str = "conversation"
    dag_creator: str = "dag_creator"
    dag_reviewer: str = "dag_reviewer"
    feedback_learner: str = "feedback_learner"


def load_config(path: str | Path | None = None) -> DagentConfig:
    config_path = Path(
        path
        or os.environ.get("DAGENT_CONFIG")
        or Path.cwd() / "config.yaml"
    )
    _load_dotenv(config_path.parent / ".env")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Config file '{config_path}' must contain a YAML mapping.")
    return DagentConfig.model_validate(data)


def dump_config(config: DagentConfig) -> dict[str, Any]:
    return config.model_dump()


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
