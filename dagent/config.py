"""Configuration models, loading helpers, and default runtime paths."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from dagent.schemas.capability import validate_capability_id_segment
from dagent.schemas.sandbox import SandboxConfig


DEFAULT_WORKSPACE = ".dagent"
DEFAULT_RUNS_DIR = "runs"


def resolve_run_workspace_root(workspace_root: str | Path, run_workspace_root: str | Path) -> Path:
    """Resolve a run root relative to the configured dagent workspace."""

    root = Path(run_workspace_root).expanduser()
    if root.is_absolute():
        return root.resolve()
    return (Path(workspace_root).expanduser() / root).resolve()


class ReasoningConfig(BaseModel):
    enabled: bool | None = None
    effort: str | None = None
    budget_tokens: int | None = None


class ProviderConfig(BaseModel):
    base_url: str
    model: str
    api_key: str | None = None
    api_key_env: str | None = None
    timeout_seconds: float = 60
    strip_thinking: bool = False
    reasoning: ReasoningConfig | None = None
    extra_request_args: dict[str, Any] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def resolve_api_key(self) -> "ProviderConfig":
        if self.api_key:
            return self
        if self.api_key_env:
            env_value = os.environ.get(self.api_key_env)
            if env_value:
                self.api_key = env_value
                self.__pydantic_fields_set__.discard("api_key")
                return self
        self.api_key = "not-needed"
        self.__pydantic_fields_set__.discard("api_key")
        return self


class DagentConfig(BaseModel):
    provider: ProviderConfig
    profiles: "ProfilesConfig" = Field(default_factory=lambda: ProfilesConfig())
    enable_result_validation: bool = False
    mcp_servers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)


class UserModelProviderConfig(ProviderConfig):
    name: str | None = None


class UserPythonToolConfig(BaseModel):
    id: str
    source: Literal["path", "managed", "module"] = "path"
    path: str | None = None
    module: str | None = None
    names: list[str] = Field(default_factory=list)
    enabled: bool = True


class UserOnlyOfficeConfig(BaseModel):
    enabled: bool = False
    document_server_url: str | None = None
    public_api_base: str | None = None
    jwt_secret: str | None = None
    lang: str = "zh"


class UserDagentConfig(BaseModel):
    mcp_servers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    model_providers: dict[str, UserModelProviderConfig] = Field(default_factory=dict)
    active_model: str | None = None
    python_tools: list[UserPythonToolConfig] = Field(default_factory=list)
    onlyoffice: UserOnlyOfficeConfig = Field(default_factory=UserOnlyOfficeConfig)


class ProfilesConfig(BaseModel):
    directory: str | None = None


def resolve_config_path(path: str | Path | None = None) -> Path:
    return Path(
        path
        or os.environ.get("DAGENT_CONFIG")
        or Path.cwd() / "config.yaml"
    ).expanduser()


def resolve_config_relative_path(value: str | Path | None, config_path: str | Path | None = None) -> Path | None:
    if value is None:
        return None
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return resolve_config_path(config_path).parent / candidate


def load_config(path: str | Path | None = None) -> DagentConfig:
    config_path = resolve_config_path(path)
    _load_dotenv(config_path.parent / ".env")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Config file '{config_path}' must contain a YAML mapping.")
    return DagentConfig.model_validate(data)


def default_user_config_path() -> Path:
    return Path.home() / ".dagent" / "config.yaml"


def load_user_config(path: str | Path | None = None) -> UserDagentConfig:
    config_path = Path(path or default_user_config_path()).expanduser()
    if not config_path.exists():
        return UserDagentConfig()
    _load_dotenv(config_path.parent / ".env")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if data is None:
        return UserDagentConfig()
    if not isinstance(data, dict):
        raise ValueError(f"User config file '{config_path}' must contain a YAML mapping.")
    return UserDagentConfig.model_validate(data)


def load_user_config_with_python_tool_errors(
    path: str | Path | None = None,
) -> tuple[UserDagentConfig, dict[str, str]]:
    config_path = Path(path or default_user_config_path()).expanduser()
    if not config_path.exists():
        return UserDagentConfig(), {}
    _load_dotenv(config_path.parent / ".env")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if data is None:
        return UserDagentConfig(), {}
    if not isinstance(data, dict):
        raise ValueError(f"User config file '{config_path}' must contain a YAML mapping.")
    raw_python_tools = data.pop("python_tools", [])
    python_tools, python_tool_errors = _validated_python_tool_configs(raw_python_tools)
    config = UserDagentConfig.model_validate({**data, "python_tools": python_tools})
    return config, python_tool_errors


def save_user_config(config: UserDagentConfig, path: str | Path | None = None) -> None:
    config_path = Path(path or default_user_config_path()).expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = _user_config_storage_data(config)
    content = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    temp_path = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(content)
        os.replace(temp_path, config_path)
        config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _user_config_storage_data(config: UserDagentConfig) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if config.mcp_servers:
        data["mcp_servers"] = config.mcp_servers
    if config.model_providers:
        data["model_providers"] = {
            name: _provider_storage_data(provider)
            for name, provider in config.model_providers.items()
        }
    if config.active_model is not None:
        data["active_model"] = config.active_model
    if config.python_tools:
        data["python_tools"] = [
            tool.model_dump(mode="json", exclude_none=True)
            for tool in config.python_tools
        ]
    onlyoffice_data = _onlyoffice_storage_data(config.onlyoffice)
    if onlyoffice_data:
        data["onlyoffice"] = onlyoffice_data
    return data


def _onlyoffice_storage_data(config: UserOnlyOfficeConfig) -> dict[str, Any]:
    data = config.model_dump(mode="json", exclude_none=True)
    if not config.enabled:
        data.pop("enabled", None)
    if config.lang == "zh":
        data.pop("lang", None)
    if not any(value is not None and value != "" for value in data.values()):
        return {}
    return data


def _validated_python_tool_configs(value: Any) -> tuple[list[UserPythonToolConfig], dict[str, str]]:
    if value is None:
        return [], {}
    if not isinstance(value, list):
        source_id = "python_tool_1"
        return [
            UserPythonToolConfig(id=source_id, source="path", enabled=False)
        ], {source_id: "python_tools must be a list."}
    configs: list[UserPythonToolConfig] = []
    errors: dict[str, str] = {}
    for index, item in enumerate(value):
        source_id = _python_tool_source_id_from_raw(item, index)
        try:
            config = UserPythonToolConfig.model_validate(item)
            if config.id != source_id:
                raise ValueError("Python tool source ids may contain only letters, numbers, and underscores.")
            configs.append(config)
        except (ValidationError, ValueError) as exc:
            configs.append(_placeholder_python_tool_config(item, source_id))
            errors[source_id] = _validation_error_message(exc) if isinstance(exc, ValidationError) else str(exc)
    return configs, errors


def _python_tool_source_id_from_raw(value: Any, index: int) -> str:
    if isinstance(value, dict):
        raw_id = str(value.get("id") or "").strip()
        try:
            return validate_capability_id_segment(raw_id, label="Python tool source ids")
        except ValueError:
            pass
    return f"python_tool_{index + 1}"


def _placeholder_python_tool_config(value: Any, source_id: str) -> UserPythonToolConfig:
    path = None
    module = None
    names: list[str] = []
    if isinstance(value, dict):
        raw_path = value.get("path")
        raw_module = value.get("module")
        raw_names = value.get("names")
        path = str(raw_path) if raw_path is not None else None
        module = str(raw_module) if raw_module is not None else None
        if isinstance(raw_names, list):
            names = [str(name) for name in raw_names]
    return UserPythonToolConfig(
        id=source_id,
        source="path",
        path=path,
        module=module,
        names=names,
        enabled=False,
    )


def _validation_error_message(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg") or str(exc))
    return f"{location}: {message}" if location else message


def _provider_storage_data(provider: ProviderConfig) -> dict[str, Any]:
    data = provider.model_dump(mode="json", exclude_none=True)
    if data.get("api_key") == "not-needed":
        data.pop("api_key", None)
    if "api_key" not in provider.model_fields_set:
        data.pop("api_key", None)
    if not data.get("extra_request_args"):
        data.pop("extra_request_args", None)
    if not data.get("extra_body"):
        data.pop("extra_body", None)
    return data


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
