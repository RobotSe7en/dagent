"""Small tool registry for bounded execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


ToolHandler = Callable[..., str]
ToolAction = str


@dataclass(frozen=True)
class Tool:
    name: str
    handler: ToolHandler
    action: ToolAction
    path_args: tuple[str, ...] = ()
    description: str = ""
    parameters: dict | None = None


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
        description: str = "",
        parameters: dict | None = None,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered.")
        self._tools[name] = Tool(
            name=name,
            handler=handler,
            action=action,
            path_args=path_args,
            description=description,
            parameters=parameters,
        )

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> set[str]:
        return set(self._tools)
