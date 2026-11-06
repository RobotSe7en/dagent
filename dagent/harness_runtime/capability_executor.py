"""Capability execution dispatcher for harness runtime."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dagent.capabilities.catalog import CapabilityCatalog
from dagent.capabilities.sandbox_context import current_run_execution
from dagent.capabilities.workspace import workspace_context
from dagent.harness_runtime.execution_usage import record_capability_call
from dagent.schemas import ArtifactState, CapabilityInvocation, CapabilityResult, DAGNode


class CapabilityExecutionError(RuntimeError):
    """Raised when a capability cannot be executed."""


def detach_containers(value: Any) -> Any:
    """Copy the plain dicts, lists and tuples in ``value``; share everything else.

    Conversation records hold tool arguments and results that user code must
    not change in place. Copying the plain containers isolates JSON data
    completely; other objects, including container subclasses such as named
    tuples, keep their type and identity.
    """

    kind = type(value)
    if kind is dict:
        return {key: detach_containers(item) for key, item in value.items()}
    if kind is list:
        return [detach_containers(item) for item in value]
    if kind is tuple:
        return tuple(detach_containers(item) for item in value)
    return value


@dataclass(frozen=True)
class CapabilityExecutionContext:
    """Runtime context supplied to context-aware capability handlers."""

    task_id: str
    dag_id: str | None = None
    node: DAGNode | None = None
    spec_id: str | None = None
    workspace_path: str | Path | None = None
    input_artifacts: dict[str, list[str | Path]] = field(default_factory=dict)
    output_artifacts: dict[str, list[str | Path]] = field(default_factory=dict)
    artifact_states: dict[str, ArtifactState] = field(default_factory=dict)
    skills: tuple[str, ...] | None = None
    extra_system_prompt: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    approved_boundary_invocation_id: str | None = None
    runtime_directory: str = ".dagent"
    max_shell_output_bytes: int = 64 * 1024 * 1024
    # False when the model reading the results cannot open saved files: oversized
    # display output is then kept inline (its display is still bounded) instead
    # of overflowing to a file.
    result_files: bool = True
    # True when the call's arguments and result belong to a conversation
    # record (tool agents, dynamic DAGs): the handler then works on copies of
    # their plain containers. Static DAG runs keep their own value handling.
    detach_values: bool = True


@dataclass(frozen=True)
class CapabilityExecutionCallbacks:
    """Streaming callbacks supplied to context-aware capability handlers."""

    on_token: Callable[[str], None] | None = None
    on_event: Callable[[dict[str, Any]], None] | None = None


class CapabilityExecutor:
    """Executes invocations against a fixed capability catalog."""

    def __init__(self, catalog: CapabilityCatalog, workspace_root: str | Path | None = None) -> None:
        self.catalog = catalog
        self.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else catalog.workspace_root

    def workspace_context(self, workspace_root: str | Path | None):
        return workspace_context(workspace_root)

    async def execute(
        self,
        invocation: CapabilityInvocation,
        *,
        context: CapabilityExecutionContext | None = None,
        callbacks: CapabilityExecutionCallbacks | None = None,
    ) -> CapabilityResult:
        workspace_path = None if context is None else context.workspace_path
        with self.workspace_context(workspace_path):
            entry = self.catalog.get_entry(invocation.capability_id)
            if entry is None:
                raise CapabilityExecutionError(f"Capability '{invocation.capability_id}' is not registered.")
            definition = entry.definition
            if not definition.enabled:
                raise CapabilityExecutionError(f"Capability '{invocation.capability_id}' is disabled.")
            if definition.kind != invocation.kind:
                raise CapabilityExecutionError(
                    f"Capability '{invocation.capability_id}' has kind '{definition.kind}', "
                    f"not '{invocation.kind}'."
                )
            if current_run_execution() == "sandbox" and entry.sandbox_execution != "builtin_tool":
                # Fail closed: only ToolCapabilityProvider-owned tools execute
                # inside the sandbox. Other handlers, including public @tool
                # bindings, run on the host and must not be allowed to fall back.
                raise CapabilityExecutionError(
                    f"Capability '{invocation.capability_id}' (kind='{invocation.kind}') cannot "
                    "run under execution='sandbox', which currently supports only built-in tool "
                    "capabilities. Use execution='local'."
                )
            # The handler gets its own copy of the arguments, so a tool that
            # changes them leaves the recorded call as the model made it.
            if context is None or context.detach_values:
                invocation = invocation.model_copy(update={"arguments": detach_containers(invocation.arguments)})
            if entry.supports_context:
                args = (invocation,)
                kwargs = {
                    "context": context,
                    "callbacks": callbacks or CapabilityExecutionCallbacks(),
                }
            else:
                args = (invocation,)
                kwargs = {}
            record_capability_call()
            if inspect.iscoroutinefunction(entry.handler):
                result = entry.handler(*args, **kwargs)
            else:
                result = await asyncio.to_thread(entry.handler, *args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
