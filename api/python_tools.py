"""User Python tool loading for the local WebUI API."""

from __future__ import annotations

import ast
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
    bindings: list[CapabilityBinding] = field(default_factory=list)
    error: str | None = None


@dataclass
class PythonToolLoadResult:
    bindings: list[CapabilityBinding]
    errors: dict[str, str]
    statuses: list[PythonToolSourceStatus] = field(default_factory=list)


def discover_python_tool_names(source: str) -> list[str]:
    """Return top-level functions decorated with @dagent.tool or @tool."""
    module = ast.parse(source)
    names: list[str] = []
    for node in module.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if any(_is_tool_decorator(decorator) for decorator in node.decorator_list):
            names.append(node.name)
    return names


def load_python_tool_sources(
    configs: Iterable[UserPythonToolConfig],
    *,
    user_config_dir: Path,
    managed_root: Path | None = None,
) -> PythonToolLoadResult:
    bindings: list[CapabilityBinding] = []
    errors: dict[str, str] = {}
    statuses: list[PythonToolSourceStatus] = []
    seen_config_ids: set[str] = set()
    resolved_managed_root = managed_root or user_config_dir / "python-tools"

    for config in configs:
        status = PythonToolSourceStatus(config=config)
        statuses.append(status)
        if not config.enabled:
            continue
        try:
            _validate_source_id(config.id, seen_config_ids)
            seen_config_ids.add(config.id)
            module = _load_source_module(
                config,
                user_config_dir=user_config_dir,
                managed_root=resolved_managed_root,
            )
            source_bindings = _bindings_from_module(module, config)
            source_capability_ids: list[str] = []
            source_seen: set[str] = set()
            for binding in source_bindings:
                capability_id = validate_capability_id(binding.definition.id, kind="tool")
                if capability_id in source_seen:
                    raise ValueError(f"Capability '{capability_id}' is exported more than once by source '{config.id}'.")
                source_seen.add(capability_id)
                source_capability_ids.append(capability_id)
            status.capability_ids.extend(source_capability_ids)
            status.bindings.extend(source_bindings)
            bindings.extend(source_bindings)
        except Exception as exc:
            status.error = str(exc)
            errors[config.id] = str(exc)

    return PythonToolLoadResult(bindings=bindings, errors=errors, statuses=statuses)


def read_python_tool_source(
    config: UserPythonToolConfig,
    *,
    user_config_dir: Path,
    managed_root: Path | None = None,
) -> str:
    if config.source == "module":
        raise ValueError("Python tool source discovery does not support module imports.")
    resolved_managed_root = managed_root or user_config_dir / "python-tools"
    path = _source_path(config, user_config_dir=user_config_dir, managed_root=resolved_managed_root)
    if not path.exists():
        raise FileNotFoundError(f"Python tool file '{path}' does not exist.")
    if not path.is_file():
        raise ValueError(f"Python tool path '{path}' is not a file.")
    if path.suffix != ".py":
        raise ValueError(f"Python tool file '{path}' must use the .py extension.")
    return path.read_text(encoding="utf-8")


def _is_tool_decorator(decorator: ast.expr) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Name):
        return target.id == "tool"
    if isinstance(target, ast.Attribute):
        return isinstance(target.value, ast.Name) and target.value.id == "dagent" and target.attr == "tool"
    return False


def _validate_source_id(value: str, seen: set[str]) -> None:
    source_id = validate_capability_id_segment(value, label="Python tool source ids")
    if source_id in seen:
        raise ValueError(f"Python tool source id '{source_id}' is duplicated.")


def _load_source_module(
    config: UserPythonToolConfig,
    *,
    user_config_dir: Path,
    managed_root: Path,
) -> ModuleType:
    if config.source == "module":
        if not config.module:
            raise ValueError(f"Python tool source '{config.id}' requires module.")
        importlib.invalidate_caches()
        if config.module in sys.modules:
            return sys.modules[config.module]
        return importlib.import_module(config.module)

    path = _source_path(config, user_config_dir=user_config_dir, managed_root=managed_root)
    if not path.exists():
        raise FileNotFoundError(f"Python tool file '{path}' does not exist.")
    if not path.is_file():
        raise ValueError(f"Python tool path '{path}' is not a file.")
    if path.suffix != ".py":
        raise ValueError(f"Python tool file '{path}' must use the .py extension.")
    return _load_module_from_path(path, source_id=config.id)


def _source_path(config: UserPythonToolConfig, *, user_config_dir: Path, managed_root: Path) -> Path:
    if not config.path:
        raise ValueError(f"Python tool source '{config.id}' requires path.")
    raw_path = Path(config.path).expanduser()
    if config.source == "managed":
        base = managed_root.expanduser().resolve()
        candidate = raw_path if raw_path.is_absolute() else user_config_dir / raw_path
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(base):
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
