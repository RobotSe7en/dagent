"""Runner-owned runtime facade for the public SDK."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from dagent.agent import AutoAgent, CapabilityRef, DagAgent, ToolAgent
from dagent.capabilities import CapabilityToolAdapter, CapabilityToolset, create_default_capability_catalog
from dagent.capabilities.catalog import CapabilityHandler
from dagent.capabilities.decorator import CapabilityBinding
from dagent.capabilities.mcp import MCPCapabilityProvider, MCPServerManager
from dagent.capabilities.providers import AgentCapabilityProvider
from dagent.capabilities.skills import SkillStore, SkillsCapabilityProvider
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
    ReviewRequiredData,
    RunFailedData,
    RunFinishedData,
    RunResult,
    RunStreamChunk,
    RunStreamEvent,
    StatusData,
    TextDeltaData,
    TraceUpdatedData,
    ValidationPassedData,
    ValidationRetryData,
    ValidationStartedData,
)
from dagent.review import ReviewDecision, ReviewLevel
from dagent.schemas import (
    CapabilityDefinition,
    CapabilityNodePayload,
    DAG,
    DAGSpec,
    PendingReview,
    RunTrace,
    RuntimeResponse,
    ValidationIssue,
)


CapabilityLike = CapabilityBinding
RunTarget = AutoAgent | ToolAgent | DagAgent | Dag | DAGSpec
SKILL_ACCESSOR_CAPABILITY_IDS = ("skill.list", "skill.view")


class Runner:
    """Owns runtime state, capability catalog, and execution dispatch."""

    def __init__(
        self,
        *,
        workspace: str | Path = ".",
        provider: ChatProvider | None = None,
        capabilities: Iterable[CapabilityLike] = (),
        validator: str | AgentProfile | ValidatorAgent | None = None,
        skill_roots: list[str | Path] | None = None,
        mcp_servers: dict[str, dict[str, Any]] | None = None,
        profile_root: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.profile_root = Path(profile_root) if profile_root is not None else None
        self._closed = False
        self._skill_provider = SkillsCapabilityProvider(skill_roots)
        self._pending_runtimes: dict[str, HarnessRuntime] = {}
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
        workspace: str | Path = ".",
        capabilities: Iterable[CapabilityLike] = (),
        validator: str | AgentProfile | ValidatorAgent | None = None,
        skill_roots: list[str | Path] | None = None,
        mcp_servers: dict[str, dict[str, Any]] | None = None,
        profile_root: str | Path | None = None,
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
        self._pending_runtimes.clear()
        self._mcp_server_capability_ids.clear()
        self._mcp_server_managers.clear()
        self._closed = True

    def add_tool(self, capability: CapabilityLike) -> CapabilityDefinition:
        """Register a single ``@dagent.tool`` binding."""

        self._ensure_open()
        definition = _register_capability(self._runtime, capability)
        self._refresh_registered_agent_runtime_configs()
        return definition

    def add_tools(self, capabilities: Iterable[CapabilityLike]) -> list[CapabilityDefinition]:
        return [self.add_tool(capability) for capability in capabilities]

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
        """Register a stdio MCP server and expose its tools as ``mcp.*`` capabilities.

        Registration is all-or-nothing: if any discovered tool fails to register
        or the server fails to connect, every capability registered by this call
        is rolled back and the server's manager is shut down before raising.
        """

        return self._add_mcp_server(name, config)

    def remove_mcp_server(self, name: str) -> None:
        """Remove a dynamically registered MCP server and its capabilities."""

        self._ensure_open()
        self._remove_mcp_server_registration(name)
        self._runtime.refresh_toolsets()
        self._refresh_registered_agent_runtime_configs()

    def replace_mcp_server(
        self,
        name: str,
        config: dict[str, Any],
    ) -> list[CapabilityDefinition]:
        """Replace a dynamically registered MCP server configuration."""

        self.remove_mcp_server(name)
        return self.add_mcp_server(name, config)

    def _add_mcp_server(
        self,
        name: str,
        config: dict[str, Any],
        *,
        manager: Any | None = None,
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
        input: Any = None,
        *,
        review: ReviewLevel | None = None,
        workspace_root: str | Path = ".dagent-runs",
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        if isinstance(target, AutoAgent):
            if input is None:
                raise TypeError("input is required for AutoAgent targets.")
            runtime = self._runtime_for_auto_agent(target)
            response = await runtime.handle_message(
                input,
                mode="auto",
                review_level=review or target.review,
                capability_scope=CapabilityScope(skills=_agent_skills(target)),
                on_token=on_token,
                on_event=on_event,
            )
            return self._run_result(runtime, response)

        if isinstance(target, ToolAgent):
            if input is None:
                raise TypeError("input is required for ToolAgent targets.")
            runtime = self._runtime_for_tool_agent(target)
            response = await runtime.handle_message(
                input,
                mode="tool",
                review_level=review or target.review,
                capability_scope=CapabilityScope(skills=_agent_skills(target)),
                on_token=on_token,
                on_event=on_event,
            )
            return self._run_result(runtime, response, kind="tool")

        if isinstance(target, DagAgent):
            if input is None:
                raise TypeError("input is required for DagAgent targets.")
            runtime = self._runtime_for_dag_agent(target)
            response = await runtime.handle_message(
                input,
                mode="dag",
                review_level=review or target.review,
                capability_scope=CapabilityScope(skills=_agent_skills(target)),
                on_token=on_token,
                on_event=on_event,
            )
            return self._run_result(runtime, response, kind="dynamic_dag")

        if isinstance(target, Dag):
            if review is not None:
                raise TypeError("review is not accepted for Dag targets.")
            self._ensure_dag_capabilities(target)
            spec = self._resolve_spec_capability_metadata(target.to_dag_spec())
            dag_run = await self._runtime.run_dag_spec(
                spec,
                input=input,
                workspace_root=workspace_root,
                artifact_uploads=artifact_uploads,
                on_token=on_token,
                on_event=on_event,
            )
            return RunResult(dag_run, kind="static_dag")

        if isinstance(target, DAGSpec):
            if review is not None:
                raise TypeError("review is not accepted for DAGSpec targets.")
            dag_run = await self._runtime.run_dag_spec(
                self._resolve_spec_capability_metadata(target),
                input=input,
                workspace_root=workspace_root,
                artifact_uploads=artifact_uploads,
                on_token=on_token,
                on_event=on_event,
            )
            return RunResult(dag_run, kind="static_dag")

        raise TypeError("Runner.run expects an AutoAgent, ToolAgent, DagAgent, Dag, or DAGSpec target.")

    async def stream(
        self,
        target: RunTarget,
        input: Any = None,
        *,
        review: ReviewLevel | None = None,
        workspace_root: str | Path = ".dagent-runs",
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
    ) -> AsyncIterator[RunStreamChunk]:
        """Run a target and yield high-level stream chunks."""

        async for event in self.stream_events(
            target,
            input,
            review=review,
            workspace_root=workspace_root,
            artifact_uploads=artifact_uploads,
        ):
            yield _chunk_from_event(event)

    async def stream_events(
        self,
        target: RunTarget,
        input: Any = None,
        *,
        review: ReviewLevel | None = None,
        workspace_root: str | Path = ".dagent-runs",
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
    ) -> AsyncIterator[RunStreamEvent]:
        """Run a target and yield low-level typed events."""

        async def run_target(on_token: TokenHandler, on_event: LoopEventHandler) -> RunResult:
            return await self.run(
                target,
                input,
                review=review,
                workspace_root=workspace_root,
                artifact_uploads=artifact_uploads,
                on_token=on_token,
                on_event=on_event,
            )

        async for event in self._stream_run(run_target):
            yield event

    async def resume_stream(
        self,
        decision: ReviewDecision,
    ) -> AsyncIterator[RunStreamChunk]:
        """Resume a pending review and yield high-level stream chunks."""

        async for event in self.resume_stream_events(decision):
            yield _chunk_from_event(event)

    async def resume_stream_events(
        self,
        decision: ReviewDecision,
    ) -> AsyncIterator[RunStreamEvent]:
        """Resume a pending review and yield low-level typed events."""

        async def run_target(on_token: TokenHandler, on_event: LoopEventHandler) -> RunResult:
            result = await self.resume(decision, on_token=on_token, on_event=on_event)
            if result is None:
                raise LookupError("Review session not found.")
            return result

        async for event in self._stream_run(run_target):
            yield event

    async def _stream_run(
        self,
        run_target: Callable[[TokenHandler, LoopEventHandler], Awaitable[RunResult]],
    ) -> AsyncIterator[RunStreamEvent]:
        queue: asyncio.Queue[RunStreamEvent] = asyncio.Queue()
        sequence = 0

        def with_sequence(event: RunStreamEvent) -> RunStreamEvent:
            nonlocal sequence
            sequence += 1
            return replace(event, sequence=sequence)

        def emit_token(token: str) -> None:
            queue.put_nowait(with_sequence(RunStreamEvent(
                type="response.output_text.delta",
                data=TextDeltaData(delta=token),
            )))

        def emit_event(event: dict[str, Any]) -> None:
            queue.put_nowait(with_sequence(_stream_event_from_runtime(event)))

        task = asyncio.create_task(run_target(emit_token, emit_event))
        try:
            while True:
                if task.done() and queue.empty():
                    break
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=0.05)
                except TimeoutError:
                    continue

            result = await task
            if result.pending_review is not None:
                yield with_sequence(_review_stream_event(result.pending_review))
            yield RunStreamEvent(
                type="run.finished",
                data=RunFinishedData(result=result),
                sequence=sequence + 1,
                run_id=result.run_id,
            )
        except Exception as exc:
            yield RunStreamEvent(
                type="run.failed",
                data=RunFailedData(message=str(exc), error_type=type(exc).__name__),
                sequence=sequence + 1,
            )
            raise
        finally:
            if not task.done():
                task.cancel()

    async def resume(
        self,
        decision: ReviewDecision,
        *,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult | None:
        runtime = self._pending_runtimes.get(decision.review_id, self._runtime)
        response = await runtime.resume_review(
            decision.review_id,
            dag=decision.dag,
            approved=decision.approved,
            review_level=decision.review_level,
            on_token=on_token,
            on_event=on_event,
        )
        if response is None:
            return None
        self._pending_runtimes.pop(decision.review_id, None)
        return self._run_result(runtime, response)

    def _run_result(
        self,
        runtime: HarnessRuntime,
        response: RuntimeResponse,
        *,
        kind: str | None = None,
    ) -> RunResult:
        if response.pending_review is not None:
            self._pending_runtimes[response.pending_review.review_id] = runtime
        return RunResult(response, kind=kind)

    def _runtime_for_auto_agent(self, agent: AutoAgent) -> HarnessRuntime:
        capability_ids = self._resolve_agent_capability_refs(agent.capabilities, agent.skills)
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
        capability_ids = self._resolve_agent_capability_refs(agent.capabilities, agent.skills)
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
        capability_ids = self._resolve_agent_capability_refs(agent.capabilities, agent.skills)
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
        register_bindings: bool = True,
    ) -> tuple[str, ...]:
        capability_ids = self._resolve_capability_refs(refs, register_bindings=register_bindings)
        return _apply_skill_capabilities(capability_ids, skills)

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
        new_agent_configs: dict[str, dict[str, Any]] = {}
        for agent in dag.agents:
            name = agent.name or ""
            existing = self._registered_agent_configs.get(name)
            if existing is not None:
                if existing != agent:
                    raise ValueError(f"Agent capability 'agent.{name}' is already registered with different config.")
                self._refresh_registered_agent_runtime_config(name, agent)
                continue
            capability_ids = self._resolve_agent_capability_refs(agent.capabilities, agent.skills)
            profile = _resolve_profile(agent.profile, profile_root=self.profile_root)
            new_agent_configs[name] = {
                "provider": self._runtime.provider,
                "profile": profile,
                "description": agent.description,
                "max_steps": agent.max_steps,
                "skills": _agent_skills(agent),
                "capability_executor": self._runtime.capability_executor,
                "tool_adapter": _tool_adapter(self._runtime.capability_catalog, capability_ids),
            }
            self._registered_agent_configs[name] = agent
            self._registered_agent_runtime_configs[name] = new_agent_configs[name]
        if new_agent_configs:
            AgentCapabilityProvider(new_agent_configs).register_into(self._runtime.capability_catalog)
            self._runtime.refresh_toolsets()

    def _refresh_registered_agent_runtime_configs(self) -> None:
        for name, agent in list(self._registered_agent_configs.items()):
            self._refresh_registered_agent_runtime_config(name, agent)

    def _refresh_registered_agent_runtime_config(self, name: str, agent: ToolAgent) -> None:
        config = self._registered_agent_runtime_configs.get(name)
        if config is None:
            return
        capability_ids = self._resolve_agent_capability_refs(
            agent.capabilities,
            agent.skills,
            register_bindings=False,
        )
        config["skills"] = _agent_skills(agent)
        config["tool_adapter"] = _tool_adapter(self._runtime.capability_catalog, capability_ids)

    def _resolve_spec_capability_metadata(self, spec: DAGSpec) -> DAGSpec:
        resolved = spec.model_copy(deep=True)
        for node in resolved.nodes:
            if not isinstance(node.payload, CapabilityNodePayload):
                continue
            invocation = node.payload.invocation
            definition = self._runtime.capability_catalog.get(invocation.capability_id)
            if definition is None:
                continue
            node.payload.invocation = invocation.model_copy(update={
                "kind": definition.kind,
                "risk": definition.policy.risk,
            })
        return resolved


def _create_runtime(
    *,
    workspace: str | Path,
    provider: ChatProvider | None,
    capabilities: Iterable[CapabilityLike],
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
    resolved_provider = provider
    catalog = create_default_capability_catalog(
        workspace_root=workspace_path,
        skills_provider=skills_provider,
    )
    capability_executor = CapabilityExecutor(catalog)
    for capability in capabilities:
        _register_capability_parts(catalog, capability)

    tool_adapter = _tool_adapter(catalog, tuple(sorted(catalog.ids())))
    capability_loop = ToolAgentLoop(
        provider=resolved_provider,
        capability_executor=capability_executor,
        tool_adapter=tool_adapter,
    )
    runtime_tool_agent = RuntimeToolAgent(
        loop=capability_loop,
        profile=_resolve_profile(tool_profile, profile_root=profile_root),
        max_steps=tool_max_steps,
    )
    dag_executor = DAGExecutor(capability_executor=capability_executor)
    dag_loop = DAGAgentLoop(
        provider=resolved_provider,
        dag_executor=dag_executor,
        tool_adapter=tool_adapter,
        max_cycles=dag_max_cycles,
    )
    runtime_dag_agent = RuntimeDAGAgent(
        loop=dag_loop,
        profile=_resolve_profile(dag_profile, profile_root=profile_root),
    )
    resolved_validator = _resolve_validator(validator, resolved_provider, profile_root=profile_root)
    return HarnessRuntime(
        provider=resolved_provider,
        tool_agent=runtime_tool_agent,
        dag_agent=runtime_dag_agent,
        validator=resolved_validator,
        enable_validation=resolved_validator is not None,
        capability_catalog=catalog,
        capability_executor=capability_executor,
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
    tool_adapter = _tool_adapter(base.capability_catalog, visible_capability_ids)
    capability_loop = ToolAgentLoop(
        provider=base.provider,
        capability_executor=base.capability_executor,
        tool_adapter=tool_adapter,
    )
    runtime_tool_agent = RuntimeToolAgent(
        loop=capability_loop,
        profile=_resolve_profile(tool_profile, profile_root=profile_root),
        max_steps=tool_max_steps,
    )
    dag_executor = DAGExecutor(capability_executor=base.capability_executor)
    dag_loop = DAGAgentLoop(
        provider=base.provider,
        dag_executor=dag_executor,
        tool_adapter=tool_adapter,
        max_cycles=dag_max_cycles,
    )
    runtime_dag_agent = RuntimeDAGAgent(
        loop=dag_loop,
        profile=_resolve_profile(dag_profile, profile_root=profile_root),
    )
    runtime = HarnessRuntime(
        provider=base.provider,
        tool_agent=runtime_tool_agent,
        dag_agent=runtime_dag_agent,
        validator=base.validator,
        enable_validation=base.enable_validation,
        max_validation_retries=base.max_validation_retries,
        capability_catalog=base.capability_catalog,
        capability_executor=base.capability_executor,
    )
    runtime.session = base.session
    runtime.tasks = base.tasks
    return runtime


def _tool_adapter(catalog, capability_ids: tuple[str, ...]) -> CapabilityToolAdapter:
    return CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset("builtin", tuple(capability_ids))],
    )


def _agent_skills(agent: AutoAgent | ToolAgent | DagAgent) -> tuple[str, ...] | None:
    if agent.skills is None:
        return None
    return tuple(agent.skills)


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
    data = dict(event)
    event_type = str(data.get("type") or "run.status")

    if event_type == "dag":
        dag = _coerce_dag(data.get("dag"))
        if dag is None:
            return RunStreamEvent(type="run.status", data=StatusData(message="DAG update was empty."))
        return RunStreamEvent(type="dag.updated", data=DagUpdatedData(dag=dag))

    if event_type == "trace":
        trace = _coerce_trace(data.get("trace"))
        if trace is None:
            return RunStreamEvent(type="run.status", data=StatusData(message="Trace update was empty."))
        return RunStreamEvent(type="trace.updated", data=TraceUpdatedData(trace=trace))

    if event_type == "review":
        review = _coerce_pending_review(data.get("review"))
        if review is not None:
            return _review_stream_event(review)
        return RunStreamEvent(type="run.status", data=StatusData(message=_status_message(data, event_type)))

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
                content=str(data.get("content") or data.get("message") or ""),
                **_capability_event_context(data),
            ),
        )

    if event_type == "validating":
        return RunStreamEvent(
            type="validation.started",
            data=ValidationStartedData(message=_status_message(data, event_type)),
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

    return RunStreamEvent(type="run.status", data=StatusData(message=_status_message(data, event_type)))


def _review_stream_event(
    review: PendingReview,
) -> RunStreamEvent:
    return RunStreamEvent(
        type="review.required",
        data=ReviewRequiredData(
            review_id=review.review_id,
            kind=review.kind,
            message=review.message,
            dag=review.proposed_dag,
            capability_call=review.capability_call,
            payload=dict(review.payload),
        ),
    )


def _chunk_from_event(event: RunStreamEvent) -> RunStreamChunk:
    if isinstance(event.data, TextDeltaData):
        return RunStreamChunk(text=event.data.delta, event=event)
    if isinstance(event.data, ReviewRequiredData):
        return RunStreamChunk(review=event.data.to_handle(), event=event)
    if isinstance(event.data, RunFinishedData):
        return RunStreamChunk(result=event.data.result, event=event)
    return RunStreamChunk(event=event)


def _capability_event_context(data: dict[str, Any]) -> dict[str, str | None]:
    return {
        key: str(data[key]) if data.get(key) is not None else None
        for key in ("task_id", "dag_id", "node_id", "parent_capability_id")
    }


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


def _coerce_pending_review(value: Any) -> PendingReview | None:
    if value is None or isinstance(value, PendingReview):
        return value
    if not isinstance(value, dict):
        return None
    proposed_dag = _coerce_dag(value.get("proposed_dag") or value.get("dag"))
    return PendingReview(
        review_id=str(value["review_id"]),
        kind=value["kind"],
        message=str(value.get("message", "")),
        proposed_dag=proposed_dag,
        capability_call=value.get("capability_call"),
        payload=dict(value.get("payload") or {}),
    )


def _status_message(data: dict[str, Any], event_type: str) -> str:
    for key in ("message", "summary", "reason", "content"):
        value = data.get(key)
        if value:
            return str(value)
    return event_type


def _register_capability(runtime: HarnessRuntime, capability: CapabilityLike) -> CapabilityDefinition:
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
    capability: CapabilityLike,
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


def _capability_parts(capability: CapabilityLike) -> tuple[CapabilityDefinition, CapabilityHandler, bool]:
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
