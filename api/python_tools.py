"""User Python tool loading for the local WebUI API."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import sys
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from dagent.capabilities.decorator import CapabilityBinding
from dagent.config import UserPythonToolConfig
from dagent.schemas import validate_capability_id, validate_capability_id_segment


@dataclass
class PythonToolSourceStatus:
    config: UserPythonToolConfig
    capability_ids: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class PythonToolLoadResult:
    bindings: list[CapabilityBinding]
    errors: dict[str, str]
    statuses: list[PythonToolSourceStatus] = field(default_factory=list)


def load_python_tool_sources(
    configs: Iterable[UserPythonToolConfig],
    *,
    user_config_dir: Path,
    existing_capability_ids: set[str] | None = None,
) -> PythonToolLoadResult:
    bindings: list[CapabilityBinding] = []
    errors: dict[str, str] = {}
    statuses: list[PythonToolSourceStatus] = []
    seen_config_ids: set[str] = set()
    seen_capability_ids = set(existing_capability_ids or set())

    for config in configs:
        status = PythonToolSourceStatus(config=config)
        statuses.append(status)
        if not config.enabled:
            continue
        try:
            _validate_source_id(config.id, seen_config_ids)
            seen_config_ids.add(config.id)
            module = _load_source_module(config, user_config_dir=user_config_dir)
            source_bindings = _bindings_from_module(module, config)
            for binding in source_bindings:
                capability_id = validate_capability_id(binding.definition.id, kind="tool")
                if capability_id in seen_capability_ids:
                    raise ValueError(f"Capability '{capability_id}' is already registered.")
                seen_capability_ids.add(capability_id)
                status.capability_ids.append(capability_id)
                bindings.append(binding)
        except Exception as exc:
            status.error = str(exc)
            errors[config.id] = str(exc)

    return PythonToolLoadResult(bindings=bindings, errors=errors, statuses=statuses)


def _validate_source_id(value: str, seen: set[str]) -> None:
    source_id = validate_capability_id_segment(value, label="Python tool source ids")
    if source_id in seen:
        raise ValueError(f"Python tool source id '{source_id}' is duplicated.")


def _load_source_module(config: UserPythonToolConfig, *, user_config_dir: Path) -> ModuleType:
    if config.source == "module":
        if not config.module:
            raise ValueError(f"Python tool source '{config.id}' requires module.")
        return importlib.import_module(config.module)

    path = _source_path(config, user_config_dir=user_config_dir)
    if not path.exists():
        raise FileNotFoundError(f"Python tool file '{path}' does not exist.")
    if not path.is_file():
        raise ValueError(f"Python tool path '{path}' is not a file.")
    if path.suffix != ".py":
        raise ValueError(f"Python tool file '{path}' must use the .py extension.")
    return _load_module_from_path(path, source_id=config.id)


def _source_path(config: UserPythonToolConfig, *, user_config_dir: Path) -> Path:
    if not config.path:
        raise ValueError(f"Python tool source '{config.id}' requires path.")
    raw_path = Path(config.path).expanduser()
    if config.source == "managed":
        base = user_config_dir.expanduser().resolve()
        candidate = raw_path if raw_path.is_absolute() else base / raw_path
        resolved = candidate.resolve(strict=False)
        if not _is_relative_to(resolved, base):
            raise ValueError(f"Managed Python tool path '{config.path}' must stay under '{base}'.")
        return resolved
    if raw_path.is_absolute():
        return raw_path.resolve(strict=False)
    return (user_config_dir / raw_path).resolve(strict=False)


def _load_module_from_path(path: Path, *, source_id: str) -> ModuleType:
    digest_input = f"{path}:{path.stat().st_mtime_ns}".encode("utf-8")
    digest = hashlib.sha256(digest_input).hexdigest()[:16]
    module_name = f"_dagent_user_tool_{source_id}_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not import Python tool file '{path}'.")
    module = importlib.util.module_from_spec(spec)
    with _temporary_syspath(path.parent):
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
    return module


def _bindings_from_module(module: ModuleType, config: UserPythonToolConfig) -> list[CapabilityBinding]:
    if not config.names:
        raise ValueError(f"Python tool source '{config.id}' must list at least one tool name.")
    bindings: list[CapabilityBinding] = []
    for name in config.names:
        export_name = validate_capability_id_segment(name, label="Python tool names")
        if not hasattr(module, export_name):
            raise ValueError(f"Python tool source '{config.id}' does not export '{export_name}'.")
        value = getattr(module, export_name)
        if not isinstance(value, CapabilityBinding):
            raise TypeError(f"Python tool '{export_name}' must be a CapabilityBinding created with @dagent.tool.")
        bindings.append(value)
    return bindings


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


@contextmanager
def _temporary_syspath(path: Path) -> Any:
    value = str(path)
    added = value not in sys.path
    if added:
        sys.path.insert(0, value)
    try:
        yield
    finally:
        if added:
            try:
                sys.path.remove(value)
            except ValueError:
                pass
