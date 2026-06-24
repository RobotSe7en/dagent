"""Runner-owned runtime facade for the public SDK."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from dagent.agent import AutoAgent, CapabilityRef, DagAgent, ToolAgent, validate_agent_name
from dagent.capabilities import CapabilityToolAdapter, CapabilityToolset, create_default_capability_catalog
from dagent.capabilities.boundaries import infer_capability_boundary
from dagent.capabilities.catalog import CapabilityHandler
from dagent.capabilities.decorator import CapabilityBinding
from dagent.capabilities.mcp import MCPCapabilityProvider, MCPServerManager
from dagent.capabilities.providers import AgentCapabilityProvider
from dagent.capabilities.sandbox import (
    SandboxExecutionError,
    SandboxSession,
    sandbox_status,
)
from dagent.capabilities.sandbox_context import (
    run_execution_context,
    sandbox_session_context,
)
from dagent.capabilities.skills import SkillStore, SkillsCapabilityProvider, visible_skills
from dagent.capabilities.workspace import workspace_context
from dagent.dag_builder import Dag
from dagent.config import load_config, resolve_config_path, resolve_config_relative_path
from dagent.harness_runtime import (
    CapabilityScope,
    CapabilityExecutor,
    DAGAgent as RuntimeDAGAgent,
    DAGAgentLoop,
    DAGExecutor,
    HarnessRuntime,
    ToolAgent as RuntimeToolAgent,
    ToolAgentLoop,
    ValidatorAgent,
)
from dagent.harness_runtime.artifacts import ArtifactUpload
from dagent.harness_runtime.tool_agent import LoopEventHandler, TokenHandler
from dagent.profiles import AgentProfile, ProfileStore, load_builtin_profile
from dagent.providers import ChatProvider, OpenAICompatibleProvider
from dagent.result import (
    CapabilityCallCompletedData,
    CapabilityCallFailedData,
    CapabilityCallStartedData,
    DagUpdatedData,
    ResponseFinishedData,
    ResponseStartedData,
    ReviewRequiredData,
    RunFailedData,
    RunFinishedData,
    RunResult,
    RunStartedData,
    RunStreamEvent,
    TextDeltaData,
    TraceUpdatedData,
    ValidationPassedData,
    ValidationRetryData,
    ValidationStartedData,
)
from dagent.review import ReviewDecision, ReviewLevel
from dagent.schemas import (
    Boundary,
    CapabilityDefinition,
    CapabilityInvocation,
    CapabilityResult,
    DAG,
    DAGSpec,
    PendingReview,
    RunExecution,
    RunState,
    RunTrace,
    SandboxConfig,
    ValidationIssue,
    iter_dag_invocations,
)
from dagent.config import DEFAULT_RUNS_DIR, DEFAULT_WORKSPACE, resolve_run_workspace_root


RunTarget = AutoAgent | ToolAgent | DagAgent | Dag | DAGSpec
SKILL_ACCESSOR_CAPABILITY_IDS = ("skill.list", "skill.view")


class Runner:
    """Owns runtime state, capability catalog, and execution dispatch."""

    def __init__(
        self,
        *,
        workspace: str | Path = DEFAULT_WORKSPACE,
        provider: ChatProvider | None = None,
        capabilities: Iterable[CapabilityBinding] = (),
        validator: str | AgentProfile | ValidatorAgent | None = None,
        skill_roots: list[str | Path] | None = None,
        mcp_servers: dict[str, dict[str, Any]] | None = None,
        profile_root: str | Path | None = None,
        sandbox: SandboxConfig | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.profile_root = Path(profile_root) if profile_root is not None else None
        self.sandbox = sandbox or SandboxConfig()
        self._closed = False
        self._skill_provider = SkillsCapabilityProvider(skill_roots)
        self._registered_agent_configs: dict[str, ToolAgent] = {}
        self._registered_agent_runtime_configs: dict[str, dict[str, Any]] = {}
        self._mcp_server_capability_ids: dict[str, tuple[str, ...]] = {}
        self._mcp_server_managers: dict[str, Any] = {}
        self._runtime = _create_runtime(
            workspace=self.workspace,
            provider=provider,
            capabilities=capabilities,
            validator=validator,
            skills_provider=self._skill_provider,
            profile_root=self.profile_root,
        )
        try:
            for name, config in dict(mcp_servers or {}).items():
                self._add_mcp_server(name, config)
        except Exception:
            self.close()
            raise

    @classmethod
    def from_config(
        cls,
        path: str | Path | None = None,
        *,
        workspace: str | Path = DEFAULT_WORKSPACE,
        capabilities: Iterable[CapabilityBinding] = (),
        validator: str | AgentProfile | ValidatorAgent | None = None,
        skill_roots: list[str | Path] | None = None,
        mcp_servers: dict[str, dict[str, Any]] | None = None,
        profile_root: str | Path | None = None,
        sandbox: SandboxConfig | None = None,
    ) -> "Runner":
        config_path = resolve_config_path(path)
        config = load_config(config_path)
        resolved_mcp_servers = dict(config.mcp_servers)
        if mcp_servers is not None:
            resolved_mcp_servers.update(mcp_servers)
        resolved_validator = validator
        if resolved_validator is None and config.enable_result_validation:
            resolved_validator = "validator_agent"
        resolved_profile_root = (
            Path(profile_root)
            if profile_root is not None
            else resolve_config_relative_path(config.profiles.directory, config_path)
        )
        return cls(
            workspace=workspace,
            provider=OpenAICompatibleProvider(config.provider),
            capabilities=capabilities,
            validator=resolved_validator,
            skill_roots=skill_roots,
            mcp_servers=resolved_mcp_servers,
            profile_root=resolved_profile_root,
            sandbox=sandbox or config.sandbox,
        )

    @property
    def runtime(self) -> HarnessRuntime:
        return self._runtime

    @property
    def capabilities(self) -> list[CapabilityDefinition]:
        return self._runtime.capability_catalog.list(enabled_only=True)

    def close(self) -> None:
        if self._closed:
            return
        self._runtime.capability_catalog.shutdown()
        self._mcp_server_capability_ids.clear()
        self._mcp_server_managers.clear()
        self._closed = True

    def add_tool(self, capability: CapabilityBinding) -> CapabilityDefinition:
        """Register a single ``@dagent.tool`` binding."""

        self._ensure_open()
        definition = _register_capability(self._runtime, capability)
        self._refresh_registered_agent_runtime_configs()
        return definition

    def add_tools(self, capabilities: Iterable[CapabilityBinding]) -> list[CapabilityDefinition]:
        return [self.add_tool(capability) for capability in capabilities]

    def add_agent(self, agent: ToolAgent) -> CapabilityDefinition:
        """Register a leaf ``ToolAgent`` as an ``agent.*`` capability."""

        self._ensure_open()
        return self._register_agent_capability(agent)

    def add_agents(self, agents: Iterable[ToolAgent]) -> list[CapabilityDefinition]:
        return [self.add_agent(agent) for agent in agents]

    def register_capability(
        self,
        definition: CapabilityDefinition,
        handler: CapabilityHandler,
        *,
        supports_context: bool = False,
    ) -> CapabilityDefinition:
        """Register a raw capability definition with an executable handler."""

        self._ensure_open()
        registered = self._runtime.register_capability(
            definition, handler, supports_context=supports_context
        )
        self._refresh_registered_agent_runtime_configs()
        return registered

    def replace_capability(
        self,
        definition: CapabilityDefinition,
        handler: CapabilityHandler,
        *,
        supports_context: bool = False,
    ) -> CapabilityDefinition:
        """Replace an already-registered capability definition and handler."""

        self._ensure_open()
        replaced = self._runtime.replace_capability(
            definition, handler, supports_context=supports_context
        )
        self._refresh_registered_agent_runtime_configs()
        return replaced

    def remove_capability(self, capability_id: str) -> None:
        """Remove a registered capability by id."""

        self._ensure_open()
        definition = self._runtime.capability_catalog.get(capability_id)
        if definition is not None and definition.kind != "agent":
            self._ensure_no_registered_agent_dependencies((capability_id,), f"remove capability '{capability_id}'")
        self._runtime.capability_catalog.delete(capability_id)
        if definition is not None and definition.kind == "agent":
            name = capability_id.removeprefix("agent.")
            self._registered_agent_configs.pop(name, None)
            self._registered_agent_runtime_configs.pop(name, None)
        self._runtime.refresh_toolsets()
        self._refresh_registered_agent_runtime_configs()

    def list_capabilities(
        self,
        *,
        kind: str | None = None,
        enabled_only: bool = False,
    ) -> list[CapabilityDefinition]:
        """List registered capability definitions."""

        return self._runtime.capability_catalog.list(kind=kind, enabled_only=enabled_only)  # type: ignore[arg-type]

    def get_capability(self, capability_id: str) -> CapabilityDefinition | None:
        """Return a registered capability definition, or ``None``."""

        return self._runtime.capability_catalog.get(capability_id)

    def set_capability_enabled(self, capability_id: str, enabled: bool) -> CapabilityDefinition:
        """Enable or disable a registered capability."""

        self._ensure_open()
        updated = self._runtime.capability_catalog.set_enabled(capability_id, enabled)
        self._runtime.refresh_toolsets()
        return updated

    async def test_capability(
        self,
        capability_id: str,
        arguments: dict[str, Any] | None = None,
        *,
        boundary: Boundary | None = None,
        execution: RunExecution = "local",
    ) -> CapabilityResult:
        """Execute a single capability once for inspection/testing."""

        definition = self._runtime.capability_catalog.get(capability_id)
        if definition is None:
            raise KeyError(f"Capability '{capability_id}' is not registered.")
        resolved_arguments = dict(arguments or {})
        resolved_boundary = (
            boundary
            if boundary is not None
            else infer_capability_boundary(definition, resolved_arguments)
        )
        invocation = CapabilityInvocation(
            capability_id=capability_id,
            kind=definition.kind,
            arguments=resolved_arguments,
            boundary=resolved_boundary,
        )
        with self._run_scope(execution):
            return await self._runtime.capability_executor.execute(invocation)

    @contextmanager
    def _run_scope(
        self,
        execution: RunExecution,
        *,
        skill_names: tuple[str, ...] | None = None,
    ) -> Iterator[None]:
        """Enter run-scoped execution context; for sandbox, manage the container."""
        if execution != "sandbox":
            with run_execution_context(execution):
                yield
            return
        if self.sandbox.backend != "docker":
            raise SandboxExecutionError(
                f"Unsupported sandbox backend: {self.sandbox.backend!r}."
            )
        workspace = self._runtime.capability_catalog.workspace_root
        session = SandboxSession(
            self.sandbox.docker,
            workspace_root=workspace,
            skill_dirs=self._sandbox_skill_dirs(skill_names),
        )
        try:
            with (
                run_execution_context("sandbox"),
                workspace_context(workspace),
                sandbox_session_context(session),
            ):
                # Start eagerly so an unavailable daemon fails at scope entry
                # with a clear error (rather than mid-run on the first tool),
                # without the redundant ping a separate precheck would add.
                session.start()
                yield
        finally:
            session.close()

    def _resolve_run_workspace_root(self, workspace_root: str | Path) -> Path:
        return resolve_run_workspace_root(self._runtime.capability_catalog.workspace_root, workspace_root)

    def _sandbox_skill_dirs(self, skill_names: tuple[str, ...] | None) -> tuple[Path, ...]:
        if not skill_names:
            return ()
        entries = visible_skills(self._skill_provider.store.list(), tuple(skill_names))
        return tuple(dict.fromkeys(Path(entry.skill_dir).resolve() for entry in entries))

    def sandbox_status(self) -> dict[str, Any]:
        """Report docker sandbox availability and configuration."""
        return sandbox_status(self.sandbox)

    @property
    def enable_validation(self) -> bool:
        return self._runtime.enable_validation

    @enable_validation.setter
    def enable_validation(self, value: bool) -> None:
        enabled = bool(value)
        if enabled and self._runtime.validator is None:
            self._runtime.validator = ValidatorAgent(
                provider=self._runtime.provider,
                profile=_resolve_profile("validator_agent", profile_root=self.profile_root),
            )
        self._runtime.enable_validation = enabled

    def run_trace(self, run_id: str) -> RunTrace | None:
        """Return the cumulative run trace for a completed/awaiting run."""

        state = self._runtime.runs.get(run_id)
        return state.trace if state is not None else None

    def run_state(self, run_id: str) -> RunState | None:
        """Return a saved run state by id."""

        state = self._runtime.runs.get(run_id)
        return None if state is None else state.model_copy(deep=True)

    @property
    def skill_store(self) -> SkillStore:
        """The filesystem-backed store powering skill discovery and installation."""

        return self._skill_provider.store

    def add_skill_root(self, root: str | Path) -> Path:
        """Add a directory to scan for skills, visible to skill.list/skill.view."""

        self._ensure_open()
        candidate = Path(root)
        existing = {Path(existing_root).resolve() for existing_root in self._skill_provider.store.roots}
        if candidate.resolve() not in existing:
            self._skill_provider.store.roots.append(candidate)
        self._sync_skill_root_metadata()
        return candidate

    def add_skill_roots(self, roots: Iterable[str | Path]) -> list[Path]:
        return [self.add_skill_root(root) for root in roots]

    def add_mcp_server(
        self,
        name: str,
        config: dict[str, Any],
    ) -> list[CapabilityDefinition]:
        """Register an MCP server and expose its tools as ``mcp.*`` capabilities.

        Registration is all-or-nothing: if any discovered tool fails to register
        or the server fails to connect, every capability registered by this call
        is rolled back and the server's manager is shut down before raising.
        """

        return self._add_mcp_server(name, config)

    def remove_mcp_server(self, name: str) -> None:
        """Remove a dynamically registered MCP server and its capabilities."""

        self._ensure_open()
        self.ensure_mcp_server_removable(name)
        self._remove_mcp_server_registration(name)
        self._runtime.refresh_toolsets()
        self._refresh_registered_agent_runtime_configs()

    def ensure_mcp_server_removable(self, name: str) -> None:
        """Raise if a registered subagent explicitly depends on an MCP server."""

        self._ensure_open()
        self._ensure_no_registered_agent_dependencies(
            self._mcp_server_capability_ids.get(name, ()),
            f"remove MCP server '{name}'",
        )

    def replace_mcp_server(
        self,
        name: str,
        config: dict[str, Any],
    ) -> list[CapabilityDefinition]:
        """Replace a dynamically registered MCP server configuration."""

        self._ensure_open()
        self._remove_mcp_server_registration(name)
        return self.add_mcp_server(name, config)

    def reload_mcp_servers(
        self,
        servers: Mapping[str, dict[str, Any]],
        *,
        replace_names: Iterable[str],
    ) -> tuple[set[str], dict[str, str]]:
        """Rebuild a group of MCP servers without treating it as user deletion."""

        self._ensure_open()
        for name in list(replace_names):
            self._remove_mcp_server_registration(name)
        registered: set[str] = set()
        errors: dict[str, str] = {}
        for name, config in servers.items():
            try:
                self._add_mcp_server(name, config, refresh=False)
                registered.add(name)
            except Exception as exc:
                errors[name] = str(exc)
        self._runtime.refresh_toolsets()
        errors.update(self._refresh_registered_agent_runtime_configs(collect_errors=True))
        return registered, errors

    def _add_mcp_server(
        self,
        name: str,
        config: dict[str, Any],
        *,
        manager: Any | None = None,
        refresh: bool = True,
    ) -> list[CapabilityDefinition]:
        self._ensure_open()
        if name in self._mcp_server_capability_ids or name in self._mcp_server_managers:
            raise ValueError(f"MCP server '{name}' is already registered.")
        if manager is None:
            available = MCPServerManager.available
        else:
            available = getattr(manager, "available", True)
        if not available:
            raise RuntimeError(
                "MCP SDK is not installed. Install dagent[mcp] to register MCP servers."
            )
        catalog = self._runtime.capability_catalog
        before = set(catalog.ids())
        provider = MCPCapabilityProvider({name: config}, manager=manager)
        try:
            provider.register_into(catalog)
            new_ids = sorted(set(catalog.ids()) - before)
            errors = list(provider.registration_errors)
            connect_error = getattr(provider.manager, "last_errors", {}).get(name)
            if connect_error:
                errors.append(f"MCP server '{name}' failed to connect: {connect_error}")
            if errors:
                raise RuntimeError("; ".join(errors))
        except Exception:
            new_ids = sorted(set(catalog.ids()) - before)
            self._rollback_mcp_registration(catalog, new_ids, getattr(provider, "manager", manager))
            raise
        self._mcp_server_capability_ids[name] = tuple(new_ids)
        provider_manager = getattr(provider, "manager", manager)
        if provider_manager is not None:
            self._mcp_server_managers[name] = provider_manager
        if refresh:
            self._runtime.refresh_toolsets()
            self._refresh_registered_agent_runtime_configs()
        return [definition for definition in (catalog.get(new_id) for new_id in new_ids) if definition is not None]

    def _remove_mcp_server_registration(self, name: str) -> None:
        catalog = self._runtime.capability_catalog
        for capability_id in self._mcp_server_capability_ids.pop(name, ()):
            catalog.delete(capability_id)
        manager = self._mcp_server_managers.pop(name, None)
        if manager is not None and hasattr(manager, "shutdown"):
            catalog.remove_shutdown_hook(manager.shutdown)
            try:
                manager.shutdown()
            except Exception:
                pass

    def _rollback_mcp_registration(self, catalog: Any, capability_ids: list[str], manager: Any) -> None:
        for capability_id in capability_ids:
            catalog.delete(capability_id)
        if manager is not None and hasattr(manager, "shutdown"):
            catalog.remove_shutdown_hook(manager.shutdown)
            try:
                manager.shutdown()
            except Exception:
                pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Runner is closed.")

    def _sync_skill_root_metadata(self) -> None:
        roots = [str(root) for root in self._skill_provider.store.roots]
        catalog = self._runtime.capability_catalog
        for capability_id in ("skill.list", "skill.view"):
            entry = catalog.get_entry(capability_id)
            if entry is None:
                continue
            updated = entry.definition.model_copy(update={"config": {**entry.definition.config, "roots": roots}})
            catalog.replace(updated, entry.handler, supports_context=entry.supports_context)

    async def run(
        self,
        target: RunTarget,
        *,
        messages: list[dict[str, Any]] | None = None,
        state: RunState | None = None,
        graph_input: Any = None,
        review: ReviewLevel | None = None,
        dynamic_adjust: bool | None = None,
        execution: RunExecution = "local",
        workspace_root: str | Path = DEFAULT_RUNS_DIR,
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        if state is not None:
            _ensure_run_state_can_continue(state)
        resolved_execution = _resolve_run_execution(execution, state)
        if resolved_execution == "sandbox" and isinstance(target, (Dag, DAGSpec, DagAgent)):
            # Preflight before entering the sandbox scope so we don't start a
            # container only to reject the run. AutoAgent resolves its mode at
            # runtime, so the _execute_loop guard remains the backstop for it.
            raise SandboxExecutionError(
                "Sandbox execution is not yet supported for DAG-based runs "
                f"({type(target).__name__}); use a tool agent or execution='local'."
            )
        skill_names = (
            _agent_skills(target)
            if isinstance(target, (AutoAgent, ToolAgent, DagAgent))
            else None
        )
        with self._run_scope(
            resolved_execution,
            skill_names=skill_names,
        ):
            return await self._run_dispatch(
                target,
                messages=messages,
                state=state,
                graph_input=graph_input,
                review=review,
                dynamic_adjust=dynamic_adjust,
                workspace_root=workspace_root,
                artifact_uploads=artifact_uploads,
                on_token=on_token,
                on_event=on_event,
            )

    async def _run_dispatch(
        self,
        target: RunTarget,
        *,
        messages: list[dict[str, Any]] | None = None,
        state: RunState | None = None,
        graph_input: Any = None,
        review: ReviewLevel | None = None,
        dynamic_adjust: bool | None = None,
        workspace_root: str | Path = DEFAULT_RUNS_DIR,
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        if isinstance(target, AutoAgent):
            if graph_input is not None:
                raise TypeError("graph_input is not accepted for AutoAgent targets.")
            run_messages = _require_messages(messages, "AutoAgent")
            runtime = self._runtime_for_auto_agent(target)
            return await runtime.handle_messages(
                run_messages,
                run_state=state,
                mode="auto",
                review_level=review or target.review,
                dynamic_adjust=target.dynamic_adjust if dynamic_adjust is None else dynamic_adjust,
                workspace_root=self._resolve_run_workspace_root(workspace_root),
                capability_scope=CapabilityScope(skills=_agent_skills(target)),
                on_token=on_token,
                on_event=on_event,
            )

        if isinstance(target, ToolAgent):
            if graph_input is not None:
                raise TypeError("graph_input is not accepted for ToolAgent targets.")
            run_messages = _require_messages(messages, "ToolAgent")
            runtime = self._runtime_for_tool_agent(target)
            return await runtime.handle_messages(
                run_messages,
                run_state=state,
                mode="tool",
                review_level=review or target.review,
                workspace_root=self._resolve_run_workspace_root(workspace_root),
                capability_scope=CapabilityScope(skills=_agent_skills(target)),
                on_token=on_token,
                on_event=on_event,
            )

        if isinstance(target, DagAgent):
            if graph_input is not None:
                raise TypeError("graph_input is not accepted for DagAgent targets.")
            run_messages = _require_messages(messages, "DagAgent")
            runtime = self._runtime_for_dag_agent(target)
            return await runtime.handle_messages(
                run_messages,
                run_state=state,
                mode="dag",
                review_level=review or target.review,
                dynamic_adjust=target.dynamic_adjust if dynamic_adjust is None else dynamic_adjust,
                workspace_root=self._resolve_run_workspace_root(workspace_root),
                capability_scope=CapabilityScope(skills=_agent_skills(target)),
                on_token=on_token,
                on_event=on_event,
            )

        if isinstance(target, Dag):
            if review is not None:
                raise TypeError("review is not accepted for Dag targets.")
            if messages is not None:
                raise TypeError("messages is not accepted for Dag targets.")
            if state is not None:
                raise TypeError("state is not accepted for Dag targets.")
            self._ensure_dag_capabilities(target)
            spec = self._resolve_spec_capability_metadata(target.to_dag_spec())
            return await self._runtime.run_dag_spec(
                spec,
                graph_input=graph_input,
                workspace_root=self._resolve_run_workspace_root(workspace_root),
                artifact_uploads=artifact_uploads,
                on_token=on_token,
                on_event=on_event,
            )

        if isinstance(target, DAGSpec):
            if review is not None:
                raise TypeError("review is not accepted for DAGSpec targets.")
            if messages is not None:
                raise TypeError("messages is not accepted for DAGSpec targets.")
            if state is not None:
                raise TypeError("state is not accepted for DAGSpec targets.")
            return await self._runtime.run_dag_spec(
                self._resolve_spec_capability_metadata(target),
                graph_input=graph_input,
                workspace_root=self._resolve_run_workspace_root(workspace_root),
                artifact_uploads=artifact_uploads,
                on_token=on_token,
                on_event=on_event,
            )

        raise TypeError("Runner.run expects an AutoAgent, ToolAgent, DagAgent, Dag, or DAGSpec target.")

    async def stream(
        self,
        target: RunTarget,
        *,
        messages: list[dict[str, Any]] | None = None,
        state: RunState | None = None,
        graph_input: Any = None,
        review: ReviewLevel | None = None,
        dynamic_adjust: bool | None = None,
        execution: RunExecution = "local",
        workspace_root: str | Path = DEFAULT_RUNS_DIR,
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
    ) -> AsyncIterator[RunStreamEvent]:
        """Run a target and yield typed stream events."""

        async def run_target(on_event: LoopEventHandler) -> RunResult:
            return await self.run(
                target,
                messages=messages,
                state=state,
                graph_input=graph_input,
                review=review,
                dynamic_adjust=dynamic_adjust,
                execution=execution,
                workspace_root=workspace_root,
                artifact_uploads=artifact_uploads,
                on_event=on_event,
            )

        async for event in self._stream_run(run_target):
            yield event

    async def resume_stream(
        self,
        decision: ReviewDecision,
        *,
        state: RunState | None = None,
        execution: RunExecution = "local",
    ) -> AsyncIterator[RunStreamEvent]:
        """Resume a pending review and yield typed stream events."""

        async def run_target(on_event: LoopEventHandler) -> RunResult:
            result = await self.resume(decision, state=state, execution=execution, on_event=on_event)
            if result is None:
                raise LookupError("Review session not found.")
            return result

        async for event in self._stream_run(run_target):
            yield event

    async def _stream_run(
        self,
        run_target: Callable[[LoopEventHandler], Awaitable[RunResult]],
    ) -> AsyncIterator[RunStreamEvent]:
        stream_done = object()
        queue: asyncio.Queue[RunStreamEvent | object] = asyncio.Queue()
        sequence = 0
        run_id: str | None = None

        def with_sequence(event: RunStreamEvent) -> RunStreamEvent:
            nonlocal sequence
            sequence += 1
            return replace(event, sequence=sequence, run_id=event.run_id or run_id)

        def emit_event(event: dict[str, Any]) -> None:
            nonlocal run_id
            stream_event = _stream_event_from_runtime(event)
            if stream_event.type == "run.started" and stream_event.run_id is not None:
                run_id = stream_event.run_id
            queue.put_nowait(with_sequence(stream_event))

        async def guarded() -> RunResult:
            try:
                return await run_target(emit_event)
            finally:
                queue.put_nowait(stream_done)

        task = asyncio.create_task(guarded())
        try:
            while True:
                item = await queue.get()
                if item is stream_done:
                    break
                yield item

            result = await task
            if result.pending_review is not None:
                yield with_sequence(
                    _review_stream_event(result.pending_review, run_id=result.run_id)
                )
            yield with_sequence(
                RunStreamEvent(
                    type="run.finished",
                    data=RunFinishedData(result=result),
                    run_id=result.run_id,
                )
            )
        except Exception as exc:
            yield with_sequence(
                RunStreamEvent(
                    type="run.failed",
                    data=RunFailedData(message=str(exc), error_type=type(exc).__name__),
                    run_id=run_id,
                )
            )
            return
        finally:
            if not task.done():
                task.cancel()

    async def resume(
        self,
        decision: ReviewDecision,
        *,
        state: RunState | None = None,
        execution: RunExecution = "local",
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult | None:
        if state is not None:
            decision = _decision_for_resume_state(decision, state)
        session_state = self._runtime.session.get_review_state(decision.review_id)
        resolved_execution = _resolve_run_execution(execution, state or session_state)
        resume_state = state or session_state
        with self._run_scope(
            resolved_execution,
            skill_names=resume_state.capability_scope.skills if resume_state is not None else None,
        ):
            return await self._runtime.resume_review(
                decision.review_id,
                run_state=state,
                dag=decision.dag,
                approved=decision.approved,
                review_level=decision.review_level,
                feedback=decision.feedback,
                on_token=on_token,
                on_event=on_event,
            )

    def _runtime_for_auto_agent(self, agent: AutoAgent) -> HarnessRuntime:
        capability_ids = self._resolve_agent_capability_refs(
            agent.capabilities,
            agent.skills,
            agents=agent.agents,
        )
        runtime = _runtime_from_existing(
            self._runtime,
            tool_profile=agent.profile,
            tool_max_steps=agent.max_steps,
            dag_profile=agent.planner_profile,
            dag_max_cycles=agent.max_cycles,
            visible_capability_ids=capability_ids,
            profile_root=self.profile_root,
        )
        return runtime

    def _runtime_for_tool_agent(self, agent: ToolAgent) -> HarnessRuntime:
        capability_ids = self._resolve_agent_capability_refs(
            agent.capabilities,
            agent.skills,
            agents=agent.agents,
        )
        runtime = _runtime_from_existing(
            self._runtime,
            tool_profile=agent.profile,
            tool_max_steps=agent.max_steps,
            dag_profile="dag_agent",
            dag_max_cycles=6,
            visible_capability_ids=capability_ids,
            profile_root=self.profile_root,
        )
        return runtime

    def _runtime_for_dag_agent(self, agent: DagAgent) -> HarnessRuntime:
        capability_ids = self._resolve_agent_capability_refs(
            agent.capabilities,
            agent.skills,
            agents=agent.agents,
        )
        runtime = _runtime_from_existing(
            self._runtime,
            tool_profile="conversation",
            tool_max_steps=8,
            dag_profile=agent.planner_profile,
            dag_max_cycles=agent.max_cycles,
            visible_capability_ids=capability_ids,
            profile_root=self.profile_root,
        )
        return runtime

    def _resolve_agent_capability_refs(
        self,
        refs: Iterable[CapabilityRef] | None,
        skills: Iterable[str] | None,
        *,
        agents: Iterable[ToolAgent | str] | str | None = None,
        register_bindings: bool = True,
    ) -> tuple[str, ...]:
        capability_ids = self._resolve_capability_refs(refs, register_bindings=register_bindings)
        agent_ids = self._resolve_agent_refs(agents)
        return _apply_skill_capabilities(tuple(dict.fromkeys((*capability_ids, *agent_ids))), skills)

    def _resolve_capability_refs(
        self,
        refs: Iterable[CapabilityRef] | None,
        *,
        register_bindings: bool = True,
    ) -> tuple[str, ...]:
        if refs is None:
            return self._default_visible_capability_ids()
        capability_ids: list[str] = []
        for ref in refs:
            if isinstance(ref, CapabilityBinding):
                definition = self.add_tool(ref) if register_bindings else ref.definition
                if self._runtime.capability_catalog.get(definition.id) is None:
                    raise KeyError(f"Capability '{definition.id}' is not registered.")
                capability_ids.append(definition.id)
            elif isinstance(ref, str):
                if self._runtime.capability_catalog.get(ref) is None:
                    raise KeyError(f"Capability '{ref}' is not registered.")
                capability_ids.append(ref)
            else:
                raise TypeError("capabilities must contain CapabilityBinding or capability id strings.")
        return tuple(dict.fromkeys(capability_ids))

    def _default_visible_capability_ids(self) -> tuple[str, ...]:
        capability_ids: list[str] = []
        for capability_id in sorted(self._runtime.capability_catalog.ids()):
            definition = self._runtime.capability_catalog.get(capability_id)
            if definition is not None and definition.kind == "agent":
                continue
            capability_ids.append(capability_id)
        return tuple(capability_ids)

    def _ensure_dag_capabilities(self, dag: Dag) -> None:
        for capability in dag.capabilities:
            self.add_tool(capability)
        for agent in dag.agents:
            self.add_agent(agent)

    def _refresh_registered_agent_runtime_configs(self, *, collect_errors: bool = False) -> dict[str, str]:
        errors: dict[str, str] = {}
        for name, agent in list(self._registered_agent_configs.items()):
            try:
                self._refresh_registered_agent_runtime_config(name, agent)
            except KeyError as exc:
                if not collect_errors:
                    raise
                errors[f"agent.{name}"] = str(exc.args[0]) if exc.args else str(exc)
        return errors

    def _refresh_registered_agent_runtime_config(self, name: str, agent: ToolAgent) -> None:
        config = self._registered_agent_runtime_configs.get(name)
        if config is None:
            return
        capability_ids = self._resolve_agent_capability_refs(
            agent.capabilities,
            agent.skills,
            register_bindings=False,
        )
        self._ensure_no_agent_capabilities(
            capability_ids,
            f"Registered subagent 'agent.{name}' cannot expose subagents",
        )
        config["skills"] = _agent_skills(agent)
        config["tool_adapter"] = _tool_adapter(self._runtime.capability_catalog, capability_ids)

    def _register_agent_capability(self, agent: ToolAgent) -> CapabilityDefinition:
        name = validate_agent_name(agent.name)
        if agent.review != "fast":
            raise ValueError("Registered subagents must use review=\"fast\".")
        existing = self._registered_agent_configs.get(name)
        capability_id = f"agent.{name}"
        if existing is not None:
            if existing != agent:
                raise ValueError(f"Agent capability '{capability_id}' is already registered with different config.")
            self._refresh_registered_agent_runtime_config(name, agent)
            definition = self._runtime.capability_catalog.get(capability_id)
            if definition is None:
                raise RuntimeError(f"Agent capability '{capability_id}' is missing from the catalog.")
            return definition

        config = self._registered_agent_runtime_config(agent)
        AgentCapabilityProvider({name: config}).register_into(self._runtime.capability_catalog)
        self._registered_agent_configs[name] = agent
        self._registered_agent_runtime_configs[name] = config
        self._runtime.refresh_toolsets()
        definition = self._runtime.capability_catalog.get(capability_id)
        if definition is None:
            raise RuntimeError(f"Agent capability '{capability_id}' was not registered.")
        return definition

    def _registered_agent_runtime_config(self, agent: ToolAgent) -> dict[str, Any]:
        name = validate_agent_name(agent.name)
        if _has_agent_refs(agent.agents):
            raise ValueError(f"Registered subagent 'agent.{name}' cannot expose subagents.")
        capability_ids = self._resolve_agent_capability_refs(agent.capabilities, agent.skills)
        self._ensure_no_agent_capabilities(
            capability_ids,
            f"Registered subagent 'agent.{name}' cannot expose subagents",
        )
        return {
            "provider": self._runtime.provider,
            "profile": _resolve_profile(agent.profile, profile_root=self.profile_root),
            "description": agent.description,
            "max_steps": agent.max_steps,
            "skills": _agent_skills(agent),
            "capability_executor": self._runtime.capability_executor,
            "tool_adapter": _tool_adapter(self._runtime.capability_catalog, capability_ids),
        }

    def _resolve_agent_refs(self, agents: Iterable[ToolAgent | str] | str | None) -> tuple[str, ...]:
        if agents is None:
            return ()
        if isinstance(agents, str):
            if agents != "registered":
                raise ValueError("agents must be 'registered' or an iterable of ToolAgent objects or agent ids.")
            return self._registered_agent_capability_ids()

        capability_ids: list[str] = []
        for ref in agents:
            if isinstance(ref, ToolAgent):
                capability_ids.append(self.add_agent(ref).id)
            elif isinstance(ref, str):
                capability_id = _agent_capability_id(ref)
                definition = self._runtime.capability_catalog.get(capability_id)
                if definition is None:
                    raise KeyError(f"Agent capability '{capability_id}' is not registered.")
                if definition.kind != "agent":
                    raise ValueError(f"Capability '{capability_id}' is not an agent capability.")
                capability_ids.append(capability_id)
            else:
                raise TypeError("agents must contain ToolAgent objects or agent capability id strings.")
        return tuple(dict.fromkeys(capability_ids))

    def _registered_agent_capability_ids(self) -> tuple[str, ...]:
        return tuple(f"agent.{name}" for name in sorted(self._registered_agent_configs))

    def _ensure_no_agent_capabilities(self, capability_ids: Iterable[str], message: str) -> None:
        nested = []
        for capability_id in capability_ids:
            definition = self._runtime.capability_catalog.get(capability_id)
            if definition is not None and definition.kind == "agent":
                nested.append(capability_id)
        if nested:
            raise ValueError(f"{message}: {', '.join(sorted(nested))}.")

    def _ensure_no_registered_agent_dependencies(self, capability_ids: Iterable[str], action: str) -> None:
        target_ids = set(capability_ids)
        if not target_ids:
            return
        dependents = [
            f"agent.{name}"
            for name, agent in sorted(self._registered_agent_configs.items())
            if target_ids.intersection(_explicit_agent_capability_ids(agent.capabilities))
        ]
        if dependents:
            joined_ids = ", ".join(sorted(target_ids))
            joined_agents = ", ".join(dependents)
            raise ValueError(f"Cannot {action}; {joined_agents} depends on {joined_ids}.")

    def _resolve_spec_capability_metadata(self, spec: DAGSpec) -> DAGSpec:
        resolved = spec.model_copy(deep=True)
        for invocation in iter_dag_invocations(resolved.nodes):
            definition = self._runtime.capability_catalog.get(invocation.capability_id)
            if definition is None:
                continue
            invocation.kind = definition.kind
            invocation.risk = definition.policy.risk
        return resolved


def _assemble_runtime(
    *,
    provider: ChatProvider,
    capability_executor: CapabilityExecutor,
    catalog: Any,
    tool_adapter: CapabilityToolAdapter,
    tool_profile: str | AgentProfile,
    tool_max_steps: int,
    dag_profile: str | AgentProfile,
    dag_max_cycles: int,
    validator: ValidatorAgent | None,
    enable_validation: bool,
    max_validation_retries: int,
    profile_root: str | Path | None,
) -> HarnessRuntime:
    runtime_tool_agent = RuntimeToolAgent(
        loop=ToolAgentLoop(
            provider=provider,
            capability_executor=capability_executor,
            tool_adapter=tool_adapter,
        ),
        profile=_resolve_profile(tool_profile, profile_root=profile_root),
        max_steps=tool_max_steps,
    )
    runtime_dag_agent = RuntimeDAGAgent(
        loop=DAGAgentLoop(
            provider=provider,
            dag_executor=DAGExecutor(
                capability_executor=capability_executor,
                capability_workspace_root=catalog.workspace_root,
            ),
            tool_adapter=tool_adapter,
            max_cycles=dag_max_cycles,
        ),
        profile=_resolve_profile(dag_profile, profile_root=profile_root),
    )
    return HarnessRuntime(
        provider=provider,
        tool_agent=runtime_tool_agent,
        dag_agent=runtime_dag_agent,
        validator=validator,
        enable_validation=enable_validation,
        max_validation_retries=max_validation_retries,
        capability_catalog=catalog,
        capability_executor=capability_executor,
    )


def _create_runtime(
    *,
    workspace: str | Path,
    provider: ChatProvider | None,
    capabilities: Iterable[CapabilityBinding],
    tool_profile: str | AgentProfile = "conversation",
    validator: str | AgentProfile | ValidatorAgent | None = None,
    tool_max_steps: int = 8,
    dag_profile: str | AgentProfile = "dag_agent",
    dag_max_cycles: int = 6,
    skills_provider: SkillsCapabilityProvider | None = None,
    profile_root: str | Path | None = None,
) -> HarnessRuntime:
    workspace_path = Path(workspace)
    if provider is None:
        raise ValueError("No provider configured. Pass provider=... or use Runner.from_config(...).")
    catalog = create_default_capability_catalog(
        workspace_root=workspace_path,
        skills_provider=skills_provider,
    )
    capability_executor = CapabilityExecutor(catalog)
    for capability in capabilities:
        _register_capability_parts(catalog, capability)

    resolved_validator = _resolve_validator(validator, provider, profile_root=profile_root)
    return _assemble_runtime(
        provider=provider,
        capability_executor=capability_executor,
        catalog=catalog,
        tool_adapter=_tool_adapter(catalog, tuple(sorted(catalog.ids()))),
        tool_profile=tool_profile,
        tool_max_steps=tool_max_steps,
        dag_profile=dag_profile,
        dag_max_cycles=dag_max_cycles,
        validator=resolved_validator,
        enable_validation=resolved_validator is not None,
        max_validation_retries=1,
        profile_root=profile_root,
    )


def _runtime_from_existing(
    base: HarnessRuntime,
    *,
    tool_profile: str | AgentProfile,
    tool_max_steps: int,
    dag_profile: str | AgentProfile,
    dag_max_cycles: int,
    visible_capability_ids: tuple[str, ...],
    profile_root: str | Path | None,
) -> HarnessRuntime:
    runtime = _assemble_runtime(
        provider=base.provider,
        capability_executor=base.capability_executor,
        catalog=base.capability_catalog,
        tool_adapter=_tool_adapter(base.capability_catalog, visible_capability_ids),
        tool_profile=tool_profile,
        tool_max_steps=tool_max_steps,
        dag_profile=dag_profile,
        dag_max_cycles=dag_max_cycles,
        validator=base.validator,
        enable_validation=base.enable_validation,
        max_validation_retries=base.max_validation_retries,
        profile_root=profile_root,
    )
    runtime.session = base.session
    runtime.runs = base.runs
    return runtime


def _tool_adapter(catalog, capability_ids: tuple[str, ...]) -> CapabilityToolAdapter:
    return CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset("builtin", tuple(capability_ids))],
    )


def _require_messages(
    messages: list[dict[str, Any]] | None,
    target_name: str,
) -> list[dict[str, Any]]:
    if messages is None:
        raise TypeError(f"messages is required for {target_name} targets.")
    if not any(message.get("role") == "user" for message in messages):
        raise ValueError("messages must contain at least one user message.")
    return [dict(message) for message in messages]


def _ensure_run_state_can_continue(state: RunState) -> None:
    if state.status == "awaiting_review" or state.pending_review is not None:
        raise ValueError(
            "Run state is awaiting review; use Runner.resume(..., state=...) "
            "to continue the pending review."
        )


def _decision_for_resume_state(decision: ReviewDecision, state: RunState) -> ReviewDecision:
    pending_review = state.pending_review
    if state.status != "awaiting_review" or pending_review is None:
        raise ValueError("resume state must be awaiting review with a pending review.")
    if pending_review.review_id != decision.review_id:
        raise ValueError(
            f"resume state review_id '{pending_review.review_id}' does not match "
            f"decision review_id '{decision.review_id}'."
        )
    if (
        decision.approved
        and decision.dag is None
        and pending_review.kind in {"initial_dag", "dag_replan"}
    ):
        if pending_review.proposed_dag is None:
            raise ValueError("Approved DAG review requires a submitted or pending proposed DAG.")
        return ReviewDecision(
            review_id=decision.review_id,
            approved=True,
            dag=pending_review.proposed_dag,
            review_level=decision.review_level,
            feedback=decision.feedback,
        )
    return decision


def _resolve_run_execution(execution: RunExecution, state: RunState | None) -> RunExecution:
    if state is None:
        return execution
    if execution != "local" and execution != state.execution:
        raise ValueError(
            f"Run state uses execution='{state.execution}', cannot override with '{execution}'."
        )
    return state.execution


def _agent_skills(agent: AutoAgent | ToolAgent | DagAgent) -> tuple[str, ...] | None:
    if agent.skills is None:
        return None
    return tuple(agent.skills)


def _has_agent_refs(agents: Iterable[ToolAgent | str] | str | None) -> bool:
    if agents is None:
        return False
    if isinstance(agents, str):
        return True
    return any(True for _ in agents)


def _explicit_agent_capability_ids(refs: Iterable[CapabilityRef] | None) -> tuple[str, ...]:
    if refs is None:
        return ()
    capability_ids: list[str] = []
    for ref in refs:
        if isinstance(ref, CapabilityBinding):
            capability_ids.append(ref.definition.id)
        elif isinstance(ref, str):
            capability_ids.append(ref)
    return tuple(dict.fromkeys(capability_ids))


def _agent_capability_id(ref: str) -> str:
    if not ref.startswith("agent."):
        raise ValueError("agent ids must use the 'agent.<name>' capability id form.")
    name = validate_agent_name(ref.removeprefix("agent."))
    return f"agent.{name}"


def _apply_skill_capabilities(
    capability_ids: tuple[str, ...],
    skills: Iterable[str] | None,
) -> tuple[str, ...]:
    ids = [capability_id for capability_id in capability_ids if capability_id not in SKILL_ACCESSOR_CAPABILITY_IDS]
    skill_scope = None if skills is None else tuple(skills)
    if skill_scope is None or skill_scope:
        ids.extend(SKILL_ACCESSOR_CAPABILITY_IDS)
    return tuple(dict.fromkeys(ids))


def _stream_event_from_runtime(event: dict[str, Any]) -> RunStreamEvent:
    """Map a runtime event payload onto the typed protocol."""
    data = dict(event)
    event_type = str(data.get("type") or "")

    if event_type == "run_started":
        return RunStreamEvent(
            type="run.started",
            data=RunStartedData(kind=str(data.get("kind") or "tool")),  # type: ignore[arg-type]
            run_id=_nullable_event_string(data.get("run_id")),
        )

    if event_type == "response_started":
        return RunStreamEvent(
            type="response.started",
            data=ResponseStartedData(**_response_event_context(data)),
        )

    if event_type == "response_token":
        channel = str(data.get("channel") or "")
        if channel not in {"reasoning", "content"}:
            raise ValueError(f"Runtime emitted unsupported response token channel: {channel!r}")
        return RunStreamEvent(
            type=f"response.{channel}.delta",  # type: ignore[arg-type]
            data=TextDeltaData(
                delta=str(data.get("delta", "")),
                **_response_event_context(data),
            ),
        )

    if event_type == "response_finished":
        return RunStreamEvent(
            type="response.finished",
            data=ResponseFinishedData(**_response_event_context(data)),
        )

    if event_type == "dag":
        dag = _coerce_dag(data.get("dag"))
        if dag is None:
            raise ValueError("Runtime emitted an empty DAG update.")
        return RunStreamEvent(type="dag.updated", data=DagUpdatedData(dag=dag))

    if event_type == "trace":
        trace = _coerce_trace(data.get("trace"))
        if trace is None:
            raise ValueError("Runtime emitted an empty trace update.")
        return RunStreamEvent(type="trace.updated", data=TraceUpdatedData(trace=trace))

    if event_type == "capability_call":
        return RunStreamEvent(
            type="capability.call.started",
            data=CapabilityCallStartedData(
                invocation_id=str(data.get("invocation_id", "")),
                capability_id=str(data.get("capability_id", "")),
                arguments=dict(data.get("arguments") or {}),
                **_capability_event_context(data),
            ),
        )

    if event_type == "capability_result":
        return RunStreamEvent(
            type="capability.call.completed",
            data=CapabilityCallCompletedData(
                invocation_id=str(data.get("invocation_id", "")),
                capability_id=str(data.get("capability_id", "")),
                content=str(data.get("content", "")),
                **_capability_event_context(data),
            ),
        )

    if event_type == "capability_error":
        return RunStreamEvent(
            type="capability.call.failed",
            data=CapabilityCallFailedData(
                invocation_id=str(data.get("invocation_id", "")),
                capability_id=str(data.get("capability_id", "")),
                content=str(data.get("content", "")),
                **_capability_event_context(data),
            ),
        )

    if event_type == "validating":
        return RunStreamEvent(
            type="validation.started",
            data=ValidationStartedData(message=str(data.get("message", ""))),
        )

    if event_type == "validation_passed":
        return RunStreamEvent(
            type="validation.passed",
            data=ValidationPassedData(
                summary=str(data.get("summary", "")),
                issues=_validation_issues(data.get("issues")),
            ),
        )

    if event_type == "retry":
        return RunStreamEvent(
            type="validation.retry",
            data=ValidationRetryData(
                summary=str(data.get("summary", "")),
                issues=_validation_issues(data.get("issues")),
                reason=str(data.get("reason", "")),
            ),
        )

    raise ValueError(f"Runtime emitted unsupported stream event type: {event_type!r}")


def _review_stream_event(
    review: PendingReview,
    *,
    run_id: str | None = None,
) -> RunStreamEvent:
    return RunStreamEvent(
        type="review.required",
        data=ReviewRequiredData(
            review_id=review.review_id,
            kind=review.kind,
            message=review.message,
        ),
        run_id=run_id,
    )


def _response_event_context(data: dict[str, Any]) -> dict[str, Any]:
    model_step = data.get("model_step")
    return {
        "response_id": str(data.get("response_id", "")),
        "model_step": int(model_step) if model_step is not None else None,
        "run_id": _nullable_event_string(data.get("run_id")),
        "dag_id": _nullable_event_string(data.get("dag_id")),
        "node_id": _nullable_event_string(data.get("node_id")),
        "parent_capability_id": _nullable_event_string(data.get("parent_capability_id")),
    }


def _capability_event_context(data: dict[str, Any]) -> dict[str, str | None]:
    return {
        "run_id": _nullable_event_string(data.get("run_id")),
        "dag_id": _nullable_event_string(data.get("dag_id")),
        "node_id": _nullable_event_string(data.get("node_id")),
        "parent_capability_id": _nullable_event_string(data.get("parent_capability_id")),
    }


def _nullable_event_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _validation_issues(value: Any) -> list[ValidationIssue]:
    issues = []
    for item in value or []:
        if isinstance(item, dict):
            issues.append(ValidationIssue(
                message=str(item.get("message", "")),
                node_id=item.get("node_id"),
            ))
    return issues


def _coerce_dag(value: Any) -> DAG | None:
    if value is None or isinstance(value, DAG):
        return value
    if isinstance(value, dict):
        return DAG.model_validate(value)
    return None


def _coerce_trace(value: Any) -> RunTrace | None:
    if value is None or isinstance(value, RunTrace):
        return value
    if isinstance(value, dict):
        return RunTrace.model_validate(value)
    return None


def _register_capability(runtime: HarnessRuntime, capability: CapabilityBinding) -> CapabilityDefinition:
    definition, handler, supports_context = _capability_parts(capability)
    _register_capability_parts(
        runtime.capability_catalog,
        capability,
        expected_handler=handler,
        expected_supports_context=supports_context,
    )
    runtime.refresh_toolsets()
    return runtime.capability_catalog.get(definition.id) or definition


def _register_capability_parts(
    catalog,
    capability: CapabilityBinding,
    *,
    expected_handler: CapabilityHandler | None = None,
    expected_supports_context: bool | None = None,
) -> None:
    definition, handler, supports_context = _capability_parts(capability)
    existing = catalog.get_entry(definition.id)
    if existing is not None:
        if (
            existing.definition == definition
            and existing.handler is (expected_handler or handler)
            and existing.supports_context == (
                supports_context if expected_supports_context is None else expected_supports_context
            )
        ):
            return
        raise ValueError(f"Capability '{definition.id}' is already registered with different config.")
    catalog.register(definition, handler, supports_context=supports_context)


def _capability_parts(capability: CapabilityBinding) -> tuple[CapabilityDefinition, CapabilityHandler, bool]:
    if not isinstance(capability, CapabilityBinding):
        raise TypeError("Expected a capability created with @dagent.tool.")
    return capability.definition, capability.handler, capability.supports_context


def _resolve_profile(
    profile: str | AgentProfile,
    *,
    profile_root: str | Path | None,
) -> AgentProfile:
    if isinstance(profile, AgentProfile):
        return profile
    if profile_root is not None:
        try:
            return ProfileStore(profile_root).load(profile)
        except FileNotFoundError:
            pass
    return load_builtin_profile(str(profile))


def _resolve_validator(
    validator: str | AgentProfile | ValidatorAgent | None,
    provider: ChatProvider,
    *,
    profile_root: str | Path | None,
) -> ValidatorAgent | None:
    if validator is None:
        return None
    if isinstance(validator, ValidatorAgent):
        return validator
    profile = (
        validator
        if isinstance(validator, AgentProfile)
        else _resolve_profile(validator, profile_root=profile_root)
    )
    return ValidatorAgent(provider=provider, profile=profile)
