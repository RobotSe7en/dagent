"""Runner-owned runtime facade for the public SDK."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, overload

from dagent.agent import CapabilityRef, DagAgent, ToolAgent
from dagent.capabilities import CapabilityToolAdapter, CapabilityToolset, create_default_capability_catalog
from dagent.capabilities.catalog import CapabilityHandler
from dagent.capabilities.decorator import CapabilityBinding
from dagent.capabilities.mcp import MCPCapabilityProvider, MCPServerManager
from dagent.capabilities.providers import AgentCapabilityProvider
from dagent.capabilities.skills import SkillStore, SkillsCapabilityProvider
from dagent.dag_builder import Dag
from dagent.config import load_config
from dagent.harness_runtime import (
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
from dagent.profiles import AgentProfile, ProfileStore
from dagent.providers import ChatProvider, OpenAICompatibleProvider
from dagent.result import RunResult
from dagent.review import ReviewDecision, ReviewLevel
from dagent.schemas import (
    CapabilityDefinition,
    CapabilityNodePayload,
    DAGRun,
    DAGSpec,
    RuntimeResponse,
)


CapabilityLike = CapabilityBinding


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
    ) -> None:
        self.workspace = Path(workspace)
        self._closed = False
        self._skill_provider = SkillsCapabilityProvider(skill_roots)
        self._runtime = _create_runtime(
            workspace=self.workspace,
            provider=provider,
            capabilities=capabilities,
            validator=validator,
            skills_provider=self._skill_provider,
            mcp_servers=mcp_servers,
        )
        self._pending_runtimes: dict[str, HarnessRuntime] = {}
        self._registered_agent_configs: dict[str, ToolAgent] = {}
        self._registered_agent_runtime_configs: dict[str, dict[str, Any]] = {}

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

    def _add_mcp_server(
        self,
        name: str,
        config: dict[str, Any],
        *,
        manager: Any | None = None,
    ) -> list[CapabilityDefinition]:
        self._ensure_open()
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
        self._runtime.refresh_toolsets()
        self._refresh_registered_agent_runtime_configs()
        return [definition for definition in (catalog.get(new_id) for new_id in new_ids) if definition is not None]

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

    @overload
    async def run(
        self,
        target: ToolAgent,
        input: str,
        *,
        review: ReviewLevel | None = None,
        workspace_root: str | Path = ".dagent-runs",
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult: ...

    @overload
    async def run(
        self,
        target: DagAgent,
        input: str,
        *,
        review: ReviewLevel | None = None,
        workspace_root: str | Path = ".dagent-runs",
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult: ...

    @overload
    async def run(
        self,
        target: Dag | DAGSpec,
        input: Any = None,
        *,
        review: ReviewLevel | None = None,
        workspace_root: str | Path = ".dagent-runs",
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> DAGRun: ...

    async def run(
        self,
        target: ToolAgent | DagAgent | Dag | DAGSpec,
        input: Any = None,
        *,
        review: ReviewLevel | None = None,
        workspace_root: str | Path = ".dagent-runs",
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult | DAGRun:
        if isinstance(target, ToolAgent):
            if input is None:
                raise TypeError("input is required for ToolAgent targets.")
            runtime = self._runtime_for_tool_agent(target)
            response = await runtime.handle_message(
                input,
                mode="tool",
                review_level=review or target.review,
                on_token=on_token,
                on_event=on_event,
            )
            return self._run_result(runtime, response)

        if isinstance(target, DagAgent):
            if input is None:
                raise TypeError("input is required for DagAgent targets.")
            runtime = self._runtime_for_dag_agent(target)
            response = await runtime.handle_message(
                input,
                mode="dag",
                review_level=review or target.review,
                on_token=on_token,
                on_event=on_event,
            )
            return self._run_result(runtime, response)

        if isinstance(target, Dag):
            if review is not None:
                raise TypeError("review is not accepted for Dag targets.")
            self._ensure_dag_capabilities(target)
            spec = self._resolve_spec_capability_metadata(target.to_dag_spec())
            return await self._runtime.run_dag_spec(
                spec,
                input=input,
                workspace_root=workspace_root,
                artifact_uploads=artifact_uploads,
                on_token=on_token,
                on_event=on_event,
            )

        if isinstance(target, DAGSpec):
            if review is not None:
                raise TypeError("review is not accepted for DAGSpec targets.")
            return await self._runtime.run_dag_spec(
                self._resolve_spec_capability_metadata(target),
                input=input,
                workspace_root=workspace_root,
                artifact_uploads=artifact_uploads,
                on_token=on_token,
                on_event=on_event,
            )

        raise TypeError("Runner.run expects a ToolAgent, DagAgent, Dag, or DAGSpec target.")

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

    def _run_result(self, runtime: HarnessRuntime, response: RuntimeResponse) -> RunResult:
        if response.pending_review is not None:
            self._pending_runtimes[response.pending_review.review_id] = runtime
        return RunResult(response)

    def _runtime_for_tool_agent(self, agent: ToolAgent) -> HarnessRuntime:
        capability_ids = self._resolve_capability_refs(agent.capabilities)
        runtime = _runtime_from_existing(
            self._runtime,
            workspace=self.workspace,
            tool_profile=agent.profile,
            tool_max_steps=agent.max_steps,
            dag_profile="dag_agent",
            dag_max_cycles=6,
            visible_capability_ids=capability_ids,
        )
        return runtime

    def _runtime_for_dag_agent(self, agent: DagAgent) -> HarnessRuntime:
        capability_ids = self._resolve_capability_refs(agent.capabilities)
        runtime = _runtime_from_existing(
            self._runtime,
            workspace=self.workspace,
            tool_profile="conversation",
            tool_max_steps=8,
            dag_profile=agent.planner_profile,
            dag_max_cycles=agent.max_cycles,
            visible_capability_ids=capability_ids,
        )
        return runtime

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
            capability_ids = self._resolve_capability_refs(agent.capabilities)
            profile = _resolve_profile(agent.profile, workspace_path=self.workspace, default_name=name or "agent")
            new_agent_configs[name] = {
                "provider": self._runtime.provider,
                "profile": profile,
                "description": agent.description,
                "max_steps": agent.max_steps,
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
        capability_ids = self._resolve_capability_refs(agent.capabilities, register_bindings=False)
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
    mcp_servers: dict[str, dict[str, Any]] | None = None,
) -> HarnessRuntime:
    workspace_path = Path(workspace)
    try:
        config = load_config()
    except FileNotFoundError:
        if provider is None:
            raise
        config = None
    if provider is not None:
        resolved_provider = provider
    else:
        assert config is not None
        resolved_provider = OpenAICompatibleProvider(config.provider)
    resolved_mcp_servers: dict[str, dict[str, Any]] = {}
    if config is not None:
        resolved_mcp_servers.update(config.mcp_servers)
    if mcp_servers is not None:
        resolved_mcp_servers.update(mcp_servers)
    catalog = create_default_capability_catalog(
        workspace_root=workspace_path,
        skills_provider=skills_provider,
        mcp_servers=resolved_mcp_servers,
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
        profile=_resolve_profile(tool_profile, workspace_path=workspace_path, default_name="conversation"),
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
        profile=_resolve_profile(dag_profile, workspace_path=workspace_path, default_name="dag_agent"),
    )
    resolved_validator = _resolve_validator(validator, resolved_provider, workspace_path=workspace_path)
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
    workspace: str | Path,
    tool_profile: str | AgentProfile,
    tool_max_steps: int,
    dag_profile: str | AgentProfile,
    dag_max_cycles: int,
    visible_capability_ids: tuple[str, ...],
) -> HarnessRuntime:
    workspace_path = Path(workspace)
    tool_adapter = _tool_adapter(base.capability_catalog, visible_capability_ids)
    capability_loop = ToolAgentLoop(
        provider=base.provider,
        capability_executor=base.capability_executor,
        tool_adapter=tool_adapter,
    )
    runtime_tool_agent = RuntimeToolAgent(
        loop=capability_loop,
        profile=_resolve_profile(tool_profile, workspace_path=workspace_path, default_name="conversation"),
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
        profile=_resolve_profile(dag_profile, workspace_path=workspace_path, default_name="dag_agent"),
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


def _resolve_provider_profile_directory(workspace_path: Path) -> Path:
    workspace_profiles = workspace_path / "profiles"
    if workspace_profiles.exists():
        return workspace_profiles
    return Path("profiles")


def _resolve_profile(
    profile: str | AgentProfile,
    *,
    workspace_path: Path,
    default_name: str,
) -> AgentProfile:
    if isinstance(profile, AgentProfile):
        return profile
    profile_path = Path(profile)
    if (profile_path / "profile.yaml").exists():
        return ProfileStore(profile_path.parent).load(profile_path.name)
    directory = _resolve_provider_profile_directory(workspace_path)
    try:
        return ProfileStore(directory).load(profile)
    except Exception:
        if profile != default_name:
            raise
        return AgentProfile(
            name=default_name,
            role=default_name,
            layers=["agent.md"],
            layer_contents={"agent.md": f"You are the {default_name}."},
        )


def _resolve_validator(
    validator: str | AgentProfile | ValidatorAgent | None,
    provider: ChatProvider,
    *,
    workspace_path: Path,
) -> ValidatorAgent | None:
    if validator is None:
        return None
    if isinstance(validator, ValidatorAgent):
        return validator
    profile = (
        validator
        if isinstance(validator, AgentProfile)
        else _resolve_profile(validator, workspace_path=workspace_path, default_name="validator_agent")
    )
    return ValidatorAgent(provider=provider, profile=profile)
