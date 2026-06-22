"""Boundary inference for capability invocations."""

from __future__ import annotations

from typing import Any

from dagent.schemas import Boundary, CapabilityDefinition


def infer_capability_boundary(
    definition: CapabilityDefinition | None,
    args: dict[str, Any],
) -> Boundary:
    if definition is None:
        return Boundary()
    config = definition.config
    checked_args = {**(config.get("default_args") or {}), **args}
    path_args = tuple(config.get("path_args") or ())
    paths = [_boundary_path_value(checked_args.get(path_arg)) for path_arg in path_args] or ["."]
    return Boundary(allowed_paths=paths)


def _boundary_path_value(value: Any) -> Any:
    if value is None or value == "":
        return "."
    if isinstance(value, dict):
        return value
    return str(value)
