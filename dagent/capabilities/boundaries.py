"""Boundary inference for capability invocations."""

from __future__ import annotations

from typing import Any

from dagent.schemas import Boundary, CapabilityDefinition


def infer_capability_boundary(
    definition: CapabilityDefinition | None,
    args: dict[str, Any],
) -> Boundary:
    if definition is None:
        return Boundary(mode="read_only")
    config = definition.config
    checked_args = {**(config.get("default_args") or {}), **args}
    path_args = tuple(config.get("path_args") or ())
    paths = [_boundary_path_value(checked_args.get(path_arg)) for path_arg in path_args] or ["."]
    action = str(config.get("action") or "read")

    if action == "write":
        return Boundary(mode="write_limited", allowed_paths=paths)
    if action == "command":
        return Boundary(mode="write_limited", allowed_paths=paths)
    return Boundary(mode="read_only", allowed_paths=paths)


def _boundary_path_value(value: Any) -> Any:
    if value is None or value == "":
        return "."
    if isinstance(value, dict):
        return value
    return str(value)
