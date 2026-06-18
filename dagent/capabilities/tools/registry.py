"""Small tool registry for bounded execution."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolOutput:
    content: str
    value: Any = None


def content_and_value_from_result(result: Any) -> tuple[str, Any]:
    """Normalize a tool handler result into ``(content, value)``.

    Single source of truth shared by the in-process executor and the sandbox
    worker. Pydantic models are handled by duck typing (``model_dump_json`` /
    ``model_dump``) so this module stays stdlib-only and importable inside the
    sandbox container, which has no pydantic.
    """
    if isinstance(result, ToolOutput):
        return result.content, result.value
    if result is None:
        return "", None
    model_dump_json = getattr(result, "model_dump_json", None)
    model_dump = getattr(result, "model_dump", None)
    if callable(model_dump_json) and callable(model_dump):
        return model_dump_json(), model_dump(mode="json")
    if isinstance(result, str):
        return result, None
    if isinstance(result, bytes):
        return result.decode("utf-8", errors="replace"), None
    if isinstance(result, tuple):
        value = list(result)
        return json.dumps(value, ensure_ascii=False), value
    if isinstance(result, (dict, list, bool, int, float)):
        return json.dumps(result, ensure_ascii=False), result
    return str(result), None


ToolResult = Any
ToolHandler = Callable[..., ToolResult]
ToolAction = str
ToolRisk = str


@dataclass(frozen=True)
class Tool:
    name: str
    handler: ToolHandler
    action: ToolAction
    path_args: tuple[str, ...] = ()
    command_args: tuple[str, ...] = ()
    default_args: dict[str, Any] | None = None
    description: str = ""
    parameters: dict | None = None
    risk: ToolRisk = "low"


class ToolRegistry:
    """Registry of callable tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        *,
        name: str,
        handler: ToolHandler,
        action: ToolAction,
        path_args: tuple[str, ...] = (),
        command_args: tuple[str, ...] = (),
        default_args: dict[str, Any] | None = None,
        description: str = "",
        parameters: dict | None = None,
        risk: ToolRisk = "low",
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered.")
        self._tools[name] = Tool(
            name=name,
            handler=handler,
            action=action,
            path_args=path_args,
            command_args=command_args,
            default_args=default_args,
            description=description,
            parameters=parameters,
            risk=risk,
        )

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> set[str]:
        return set(self._tools)
