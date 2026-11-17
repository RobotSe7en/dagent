"""Public opt-in user interaction API; no transport or UI implementation."""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

from dagent.schemas.user_interactions import (
    ConfirmParameters, EditorParameters, InputParameters, SelectParameters,
    UserInteractionAbandonment, UserInteractionAnswer, UserInteractionEnded,
    UserInteractionOption, UserInteractionRequest, UserInteractionResponse,
)

if TYPE_CHECKING:
    from dagent.capabilities.decorator import CapabilityBinding
    from dagent.result import RunResult


class UserInteractionError(RuntimeError):
    """Base error for user interaction operations."""


class UserInteractionCancelled(UserInteractionError):
    """The user abandoned this interaction, without stopping the run."""


class UserInteractionUnsupported(UserInteractionError):
    """This invocation has no supported interaction receiver."""


class UserInteractionBusy(UserInteractionError):
    """The run already has a pending interaction."""


class UserInteractionResponseRejected(UserInteractionError):
    """A response was rejected without consuming another pending request."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class UserInteractionFatalError(UserInteractionError):
    """An interaction failure that must stop subsequent run dispatch."""

    result: RunResult | None = None


class UserInteractionTimeout(UserInteractionFatalError):
    """The user interaction deadline expired."""


class UserInteractionUnavailable(UserInteractionFatalError):
    """The host can no longer complete the interaction."""


class UserInteractionDeliveryError(UserInteractionFatalError):
    """A synchronous interaction notification failed."""


@dataclass(frozen=True)
class UserInteractionOptions:
    """Per-execution receiver and optional per-interaction deadline.

    Callbacks must synchronously enqueue notifications and return promptly.
    The host performs asynchronous work separately and submits responses through
    the owning Runner on its execution event loop.
    """

    on_request: Callable[[UserInteractionRequest], None]
    on_end: Callable[[UserInteractionEnded], None] | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        for callback in (self.on_request,) + (() if self.on_end is None else (self.on_end,)):
            if not callable(callback) or inspect.iscoroutinefunction(callback):
                raise TypeError("Interaction notifications require synchronous callbacks.")
        timeout = self.timeout_seconds
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number or None.")


class UserInteractionClient:
    """Invocation-bound interface exposed as ``context.ui``.

    A context without an active, supported receiver rejects requests immediately.
    """

    async def _request(self, parameters: ConfirmParameters | SelectParameters | InputParameters | EditorParameters) -> bool | str:
        raise UserInteractionUnsupported("User interaction requires a local root ToolLoop and a receiver.")

    async def confirm(self, title: str, description: str | None = None) -> bool:
        return cast(bool, await self._request(ConfirmParameters(title=title, description=description)))

    async def select(self, title: str, options: Sequence[UserInteractionOption]) -> str:
        return cast(str, await self._request(SelectParameters(title=title, options=tuple(options))))

    async def input(self, title: str, prompt: str = "", initial: str = "") -> str:
        return cast(str, await self._request(InputParameters(title=title, prompt=prompt, initial=initial)))

    async def editor(self, title: str, content: str, format: Literal["text", "markdown", "html"] = "text") -> str:
        return cast(str, await self._request(EditorParameters(title=title, content=content, format=format)))


class ToolContext(Protocol):
    """Public context view for ``supports_context=True`` Python tools."""

    @property
    def task_id(self) -> str: ...

    @property
    def workspace_path(self) -> str | Path | None: ...

    @property
    def ui(self) -> UserInteractionClient: ...


def ui_tools() -> tuple[CapabilityBinding, ...]:
    """Create four separately registerable, opt-in model interaction tools."""
    from dagent.capabilities.user_interactions import interaction_tools

    return interaction_tools()
