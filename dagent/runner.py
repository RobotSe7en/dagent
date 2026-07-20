"""Runner-owned runtime facade for the public SDK."""

from __future__ import annotations

import asyncio
import threading
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from dagent.agent import AutoAgent, CapabilityRef, DagAgent, ToolAgent, validate_agent_name
from dagent.capabilities import CapabilityToolAdapter, CapabilityToolset, create_default_capability_catalog
from dagent.capabilities.boundaries import infer_capability_boundary
from dagent.capabilities.cancellation import run_cancellation_context
from dagent.capabilities.catalog import CapabilityHandler
from dagent.capabilities.decorator import CapabilityBinding
from dagent.capabilities.mcp import MCPCapabilityProvider, MCPServerManager
from dagent.capabilities.providers import AgentCapabilityProvider
from dagent.capabilities.python_tools import (
    load_python_tool_sources,
)
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
from dagent.capabilities.toolsets import BUILTIN_CAPABILITY_IDS
from dagent.capabilities.workspace import workspace_context
from dagent.dag_builder import Dag
from dagent.config import (
    DEFAULT_RUNS_DIR,
    DEFAULT_WORKSPACE,
    UserPythonToolConfig,
    load_config,
    resolve_config_path,
    resolve_config_relative_path,
    resolve_run_workspace_root,
)
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
from dagent.harness_runtime.execution_budget import (
    ExecutionBudget,
    ExecutionLimitExceeded,
    execution_budget_scope,
)
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
    ExecutionLimits,
    ExecutionUsage,
    MCPServerRegistrationResult,
    MCPServerSnapshot,
    MCPToolSnapshot,
    PendingReview,
    PythonToolRegistrationResult,
    PythonToolSourceRegistrationStatus,
    RunnerCatalogView,
    RunExecution,
    ResolvedRunPlan,
    RunCapabilityScope,
    RunCheckpoint,
    RunState,
    RunTrace,
    SandboxConfig,
    ValidationIssue,
    ValidationResult,
    iter_dag_invocations,
)
from dagent.schemas.run_id import validate_run_id

RunTarget = AutoAgent | ToolAgent | DagAgent | Dag | DAGSpec
SKILL_ACCESSOR_CAPABILITY_IDS = ("skill.list", "skill.view")


@dataclass(frozen=True)
class _ResolvedRuntime:
    runtime: HarnessRuntime
    capability_ids: tuple[str, ...]
    skill_ids: tuple[str, ...]


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
        self._run_checkpoints: dict[str, RunCheckpoint] = {}
        self._active_run_tasks: dict[str, asyncio.Task[RunResult]] = {}
        self._active_run_cancellation_events: dict[str, threading.Event] = {}
        self._consumed_review_ids: set[str] = set()
        self._mcp_server_capability_ids: dict[str, tuple[str, ...]] = {}
        self._mcp_server_managers: dict[str, Any] = {}
        initial_capabilities = list(capabilities)
        self._local_tool_binding_ids: set[str] = set()
        self._runtime = _create_runtime(
            workspace=self.workspace,
            provider=provider,
            capabilities=initial_capabilities,
            validator=validator,
            skills_provider=self._skill_provider,
            profile_root=self.profile_root,
        )
        self._local_tool_binding_ids.update(
            _capability_parts(capability)[0].id
            for capability in initial_capabilities
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
        for event in self._active_run_cancellation_events.values():
            event.set()
        for task in self._active_run_tasks.values():
            task.cancel()
        self._active_run_tasks.clear()
        self._active_run_cancellation_events.clear()
        self._runtime.capability_catalog.shutdown()
        self._run_checkpoints.clear()
        self._consumed_review_ids.clear()
        self._mcp_server_capability_ids.clear()
        self._mcp_server_managers.clear()
        self._closed = True

    def derive(
        self,
        *,
        workspace: str | Path | None = None,
        provider: ChatProvider | None = None,
        capabilities: Iterable[CapabilityBinding] = (),
        validator: str | AgentProfile | ValidatorAgent | None = None,
        enable_validation: bool | None = None,
        max_validation_retries: int | None = None,
        skill_roots: list[str | Path] | None = None,
        mcp_servers: dict[str, dict[str, Any]] | None = None,
        profile_root: str | Path | None = None,
        sandbox: SandboxConfig | None = None,
        agents: Iterable[ToolAgent] = (),
        inherit_local_tools: bool = False,
        exclude_local_tool_ids: Iterable[str] = (),
    ) -> "Runner":
        """Create an independent runner with explicit runtime overlays."""

        self._ensure_open()
        overlay_capabilities = list(capabilities)
        if inherit_local_tools:
            overlay_capabilities = [
                *self._local_tool_bindings(exclude_ids=exclude_local_tool_ids),
                *overlay_capabilities,
            ]
        resolved_provider = provider or self._runtime.provider
        resolved_profile_root = (
            Path(profile_root)
            if profile_root is not None
            else self.profile_root
        )
        resolved_skill_roots = (
            list(skill_roots)
            if skill_roots is not None
            else list(self._skill_provider.store.roots)
        )
        resolved_validator = validator
        if resolved_validator is None and self._runtime.enable_validation:
            resolved_validator = (
                self._runtime.validator
                if resolved_provider is self._runtime.provider
                else "validator_agent"
            )
        derived = Runner(
            workspace=self._runtime.capability_catalog.workspace_root if workspace is None else workspace,
            provider=resolved_provider,
            capabilities=overlay_capabilities,
            validator=resolved_validator,
            skill_roots=resolved_skill_roots,
            mcp_servers=mcp_servers,
            profile_root=resolved_profile_root,
            sandbox=sandbox if sandbox is not None else self.sandbox.model_copy(deep=True),
        )
        try:
            derived.runtime.max_validation_retries = (
                self._runtime.max_validation_retries
                if max_validation_retries is None
                else max_validation_retries
            )
            if enable_validation is not None:
                derived.enable_validation = enable_validation
            elif not self._runtime.enable_validation:
                derived.enable_validation = False
            for agent in agents:
                derived.add_agent(agent)
        except Exception:
            derived.close()
            raise
        return derived

    def add_tool(self, capability: CapabilityBinding) -> CapabilityDefinition:
        """Register a single ``@dagent.tool`` binding."""

        self._ensure_open()
        definition = _register_capability(self._runtime, capability)
        self._local_tool_binding_ids.add(definition.id)
        self._refresh_registered_agent_runtime_configs()
        return definition

    def add_tools(self, capabilities: Iterable[CapabilityBinding]) -> list[CapabilityDefinition]:
        """Register ``@dagent.tool`` bindings as one atomic batch."""

        self._ensure_open()
        definitions = self._add_tools(capabilities, refresh=True)
        self._local_tool_binding_ids.update(definition.id for definition in definitions)
        return definitions

    def reload_tools(
        self,
        groups: Mapping[str, Iterable[CapabilityBinding]],
        *,
        replace_ids: Iterable[str],
    ) -> tuple[dict[str, list[CapabilityDefinition]], dict[str, str]]:
        """Rebuild caller-managed Python tool groups without treating removals as user deletion."""

        replace_id_set = self._validate_reload_tool_replace_ids(replace_ids)
        for capability_id in replace_id_set:
            self._runtime.capability_catalog.delete(capability_id)
            self._local_tool_binding_ids.discard(capability_id)
        registered: dict[str, list[CapabilityDefinition]] = {}
        errors: dict[str, str] = {}
        for group_id, capabilities in groups.items():
            try:
                definitions = self._add_tools(capabilities, refresh=False)
                registered[group_id] = definitions
                self._local_tool_binding_ids.update(definition.id for definition in definitions)
            except Exception as exc:
                errors[group_id] = str(exc)
        self._runtime.refresh_toolsets()
        errors.update(self._refresh_registered_agent_runtime_configs(collect_errors=True))
        return registered, errors

    def _validate_reload_tool_replace_ids(self, replace_ids: Iterable[str]) -> set[str]:
        self._ensure_open()
        catalog = self._runtime.capability_catalog
        replace_id_set = set(replace_ids)
        for capability_id in replace_id_set:
            definition = catalog.get(capability_id)
            if definition is None:
                continue
            if definition.kind != "tool":
                raise ValueError("reload_tools can only replace tool capabilities.")
            if capability_id in BUILTIN_CAPABILITY_IDS:
                raise ValueError(f"reload_tools cannot replace built-in capability '{capability_id}'.")
        return replace_id_set

    def reload_python_tool_sources(
        self,
        configs: Iterable[UserPythonToolConfig],
        *,
        user_config_dir: str | Path,
        managed_root: str | Path | None = None,
        replace_ids: Iterable[str],
    ) -> PythonToolRegistrationResult:
        """Load configured Python tool sources and rebuild their registered groups."""

        replace_id_set = self._validate_reload_tool_replace_ids(replace_ids)
        load_result = load_python_tool_sources(
            configs,
            user_config_dir=Path(user_config_dir),
            managed_root=None if managed_root is None else Path(managed_root),
        )
        groups = {
            status.config.id: status.bindings
            for status in load_result.statuses
            if status.error is None and status.config.enabled
        }
        registered, registration_errors = self.reload_tools(groups, replace_ids=replace_id_set)
        source_registration_errors = {
            source_id: error
            for source_id, error in registration_errors.items()
            if source_id in groups
        }
        capability_ids_by_source = {
            status.config.id: list(status.capability_ids)
            for status in load_result.statuses
        }
        for source_id in source_registration_errors:
            capability_ids_by_source[source_id] = []
        errors = {
            **load_result.errors,
            **registration_errors,
        }
        return PythonToolRegistrationResult(
            statuses=[
                PythonToolSourceRegistrationStatus(
                    source_id=status.config.id,
                    enabled=status.config.enabled,
                    capability_ids=capability_ids_by_source.get(status.config.id, []),
                    error=errors.get(status.config.id),
                )
                for status in load_result.statuses
            ],
            registered=registered,
            errors=errors,
            capability_ids_by_source=capability_ids_by_source,
        )

    def _add_tools(
        self,
        capabilities: Iterable[CapabilityBinding],
        *,
        refresh: bool,
    ) -> list[CapabilityDefinition]:
        bindings = list(capabilities)
        catalog = self._runtime.capability_catalog
        _validate_capability_binding_batch(catalog, bindings)
        registered_ids: list[str] = []
        definitions: list[CapabilityDefinition] = []
        try:
            for capability in bindings:
                registered = _register_capability_parts(catalog, capability)
                definition = capability.definition
                if registered:
                    registered_ids.append(definition.id)
                definitions.append(catalog.get(definition.id) or definition)
        except Exception:
            for capability_id in reversed(registered_ids):
                catalog.delete(capability_id)
            if refresh:
                self._runtime.refresh_toolsets()
                self._refresh_registered_agent_runtime_configs()
            raise
        if refresh:
            self._runtime.refresh_toolsets()
            self._refresh_registered_agent_runtime_configs()
        return definitions

    def validate_tools_registerable(
        self,
        capabilities: Iterable[CapabilityBinding],
        *,
        ignore_ids: Iterable[str] = (),
    ) -> None:
        """Validate a batch of tool bindings without mutating runtime state."""

        self._ensure_open()
        _validate_capability_binding_batch(
            self._runtime.capability_catalog,
            list(capabilities),
            ignore_ids=ignore_ids,
        )

    def add_agent(self, agent: ToolAgent) -> CapabilityDefinition:
        """Register a leaf ``ToolAgent`` as an ``agent.*`` capability."""

        self._ensure_open()
        return self._register_agent_capability(agent)

    def add_agents(self, agents: Iterable[ToolAgent]) -> list[CapabilityDefinition]:
        return [self.add_agent(agent) for agent in agents]

    def validate_agent_registration(self, agent: ToolAgent, *, replacing: bool = False) -> None:
        """Validate that a leaf ``ToolAgent`` can be registered without mutating runtime state."""

        self._ensure_open()
        self._validate_agent_registration(agent, replacing=replacing)

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

    def validate_capability_registerable(self, definition: CapabilityDefinition) -> None:
        """Validate a raw capability definition without mutating runtime state."""

        self._ensure_open()
        self._runtime.capability_catalog.validate_registerable(definition)

    def validate_capability_refs(
        self,
        refs: Iterable[CapabilityRef] | None,
        *,
        allowed_kinds: Iterable[str] | None = None,
        allow_agents: bool = False,
        enabled_only: bool = True,
    ) -> ValidationResult:
        """Validate capability references without registering tools or agents."""

        self._ensure_open()
        issues: list[ValidationIssue] = []
        allowed_kind_set = None if allowed_kinds is None else set(allowed_kinds)
        refs_to_check = self._default_visible_capability_ids() if refs is None else refs
        for ref in refs_to_check:
            if isinstance(ref, CapabilityBinding):
                definition, handler, supports_context = _capability_parts(ref)
                capability_id = definition.id
                entry = self._runtime.capability_catalog.get_entry(capability_id)
                if entry is not None and not _entry_matches_binding(
                    entry,
                    definition,
                    handler,
                    supports_context,
                ):
                    issues.append(ValidationIssue(
                        message=(
                            f"Capability binding '{capability_id}' conflicts "
                            "with a registered capability."
                        ),
                        capability_id=capability_id,
                        code="binding_conflict",
                    ))
                    continue
                definition = None if entry is None else entry.definition
            elif isinstance(ref, str):
                capability_id = ref
                definition = self._runtime.capability_catalog.get(capability_id)
            else:
                issues.append(ValidationIssue(
                    message="Capability refs must be capability id strings or @dagent.tool bindings.",
                    code="invalid_ref_type",
                ))
                continue
            if definition is None:
                issues.append(ValidationIssue(
                    message=f"Capability '{capability_id}' is not registered.",
                    capability_id=capability_id,
                    code="unknown_capability",
                ))
                continue
            if enabled_only and not definition.enabled:
                issues.append(ValidationIssue(
                    message=f"Capability '{capability_id}' is disabled.",
                    capability_id=capability_id,
                    code="disabled_capability",
                ))
                continue
            if definition.kind == "agent" and not allow_agents:
                issues.append(ValidationIssue(
                    message=f"Capability '{capability_id}' is an agent capability.",
                    capability_id=capability_id,
                    code="agent_not_allowed",
                ))
                continue
            if allowed_kind_set is not None and definition.kind not in allowed_kind_set:
                issues.append(ValidationIssue(
                    message=f"Capability '{capability_id}' kind '{definition.kind}' is not allowed.",
                    capability_id=capability_id,
                    code="unsupported_kind",
                ))
        return ValidationResult(passed=not issues, issues=issues)

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
        self._local_tool_binding_ids.discard(definition.id)
        self._refresh_registered_agent_runtime_configs()
        return replaced

    def remove_capability(self, capability_id: str) -> None:
        """Remove a registered capability by id."""

        self._ensure_open()
        definition = self._runtime.capability_catalog.get(capability_id)
        if definition is not None and definition.kind != "agent":
            self._ensure_no_registered_agent_dependencies((capability_id,), f"remove capability '{capability_id}'")
        self._runtime.capability_catalog.delete(capability_id)
        self._local_tool_binding_ids.discard(capability_id)
        if definition is not None and definition.kind == "agent":
            name = capability_id.removeprefix("agent.")
            self._registered_agent_configs.pop(name, None)
            self._registered_agent_runtime_configs.pop(name, None)
            self._runtime.refresh_toolsets()
            return
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

    def _local_tool_bindings(self, *, exclude_ids: Iterable[str] = ()) -> list[CapabilityBinding]:
        catalog = self._runtime.capability_catalog
        excluded = set(exclude_ids)
        bindings: list[CapabilityBinding] = []
        for capability_id in sorted(self._local_tool_binding_ids - excluded):
            entry = catalog.get_entry(capability_id)
            if entry is None or entry.definition.kind != "tool":
                continue
            bindings.append(CapabilityBinding(
                definition=entry.definition,
                handler=entry.handler,
                supports_context=entry.supports_context,
            ))
        return bindings

    def catalog_view(
        self,
        *,
        kind: str | None = None,
        enabled_only: bool = False,
    ) -> RunnerCatalogView:
        """Return a read-only view of registered runtime capabilities."""

        return RunnerCatalogView(
            workspace_root=str(self._runtime.capability_catalog.workspace_root),
            capabilities=self.list_capabilities(kind=kind, enabled_only=enabled_only),
            mcp_servers=(
                self.list_mcp_server_snapshots(enabled_only=enabled_only)
                if kind in {None, "mcp"}
                else []
            ),
        )

    def get_capability(self, capability_id: str) -> CapabilityDefinition | None:
        """Return a registered capability definition, or ``None``."""

        return self._runtime.capability_catalog.get(capability_id)

    def set_capability_enabled(self, capability_id: str, enabled: bool) -> CapabilityDefinition:
        """Enable or disable a registered capability."""

        self._ensure_open()
        definition = self._runtime.capability_catalog.get(capability_id)
        if definition is not None and not enabled and definition.kind != "agent":
            self._ensure_no_registered_agent_dependencies((capability_id,), f"disable capability '{capability_id}'")
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

    def run_checkpoint(self, run_id: str) -> RunCheckpoint | None:
        """Return the latest checkpoint, including terminal resume failures."""

        checkpoint = self._run_checkpoints.get(run_id)
        return None if checkpoint is None else checkpoint.model_copy(deep=True)

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
        *,
        snapshot: MCPServerSnapshot | None = None,
        lazy_connect: bool = False,
    ) -> list[CapabilityDefinition]:
        """Register an MCP server and expose its tools as ``mcp.*`` capabilities.

        Registration is all-or-nothing: if any discovered tool fails to register
        or the server fails to connect, every capability registered by this call
        is rolled back and the server's manager is shut down before raising.
        """

        return self._add_mcp_server(
            name,
            config,
            snapshot=snapshot,
            lazy_connect=lazy_connect,
        )

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
        catalog = self._runtime.capability_catalog
        old_ids = self._mcp_server_capability_ids.get(name, ())
        old_entries = {
            capability_id: catalog.get_entry(capability_id)
            for capability_id in old_ids
        }
        old_manager = self._mcp_server_managers.get(name)
        self._remove_mcp_server_registration(name, shutdown=False)
        try:
            definitions = self._add_mcp_server(name, config, refresh=False)
            new_ids = self._mcp_server_capability_ids.get(name, ())
            missing_ids = set(old_ids) - set(new_ids)
            self._ensure_no_registered_agent_dependencies(
                missing_ids,
                f"replace MCP server '{name}'",
            )
            self._runtime.refresh_toolsets()
            self._refresh_registered_agent_runtime_configs()
        except Exception:
            self._remove_mcp_server_registration(name)
            self._restore_mcp_server_registration(name, old_entries, old_manager)
            self._runtime.refresh_toolsets()
            self._refresh_registered_agent_runtime_configs()
            raise
        if old_manager is not None and hasattr(old_manager, "shutdown"):
            try:
                old_manager.shutdown()
            except Exception:
                pass
        return definitions

    def reload_mcp_servers(
        self,
        servers: Mapping[str, dict[str, Any]],
        *,
        replace_names: Iterable[str],
        snapshots: Mapping[str, MCPServerSnapshot] | None = None,
        lazy_connect: bool = False,
    ) -> tuple[set[str], dict[str, str]]:
        """Rebuild a group of MCP servers without treating it as user deletion."""

        self._ensure_open()
        snapshot_map = dict(snapshots or {})
        for name in list(replace_names):
            self._remove_mcp_server_registration(name)
        registered: set[str] = set()
        errors: dict[str, str] = {}
        for name, config in servers.items():
            try:
                self._add_mcp_server(
                    name,
                    config,
                    refresh=False,
                    snapshot=snapshot_map.get(name),
                    lazy_connect=lazy_connect,
                )
                registered.add(name)
            except Exception as exc:
                errors[name] = str(exc)
        self._runtime.refresh_toolsets()
        errors.update(self._refresh_registered_agent_runtime_configs(collect_errors=True))
        return registered, errors

    def reload_mcp_servers_with_snapshots(
        self,
        servers: Mapping[str, dict[str, Any]],
        *,
        replace_names: Iterable[str],
        snapshots: Mapping[str, MCPServerSnapshot] | None = None,
        lazy_connect: bool = False,
    ) -> MCPServerRegistrationResult:
        """Rebuild MCP servers and return registration snapshots for successes."""

        registered, errors = self.reload_mcp_servers(
            servers,
            replace_names=replace_names,
            snapshots=snapshots,
            lazy_connect=lazy_connect,
        )
        snapshots = [
            snapshot
            for name in sorted(registered)
            if (snapshot := self.mcp_server_snapshot(name)) is not None
        ]
        return MCPServerRegistrationResult(
            registered_names=sorted(registered),
            snapshots=snapshots,
            errors=errors,
        )

    def mcp_server_snapshot(self, name: str, *, enabled_only: bool = False) -> MCPServerSnapshot | None:
        """Return a read-only snapshot of one registered MCP server."""

        capability_ids = self._mcp_server_capability_ids.get(name)
        if capability_ids is None:
            return None
        tools: list[MCPToolSnapshot] = []
        for capability_id in capability_ids:
            definition = self._runtime.capability_catalog.get(capability_id)
            if definition is None:
                continue
            if enabled_only and not definition.enabled:
                continue
            tools.append(
                MCPToolSnapshot(
                    capability_id=definition.id,
                    server=str(definition.config.get("server") or ""),
                    tool=str(definition.config.get("tool") or ""),
                    definition=definition,
                )
            )
        return MCPServerSnapshot(
            name=name,
            capability_ids=[tool.capability_id for tool in tools],
            tools=tools,
        )

    def list_mcp_server_snapshots(self, *, enabled_only: bool = False) -> list[MCPServerSnapshot]:
        """Return read-only snapshots for registered MCP servers."""

        return [
            snapshot
            for name in sorted(self._mcp_server_capability_ids)
            if (snapshot := self.mcp_server_snapshot(name, enabled_only=enabled_only)) is not None
            and (snapshot.tools or not enabled_only)
        ]

    def _add_mcp_server(
        self,
        name: str,
        config: dict[str, Any],
        *,
        manager: Any | None = None,
        snapshot: MCPServerSnapshot | None = None,
        lazy_connect: bool = False,
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
        provider_kwargs: dict[str, Any] = {"manager": manager}
        if snapshot is not None or lazy_connect:
            provider_kwargs["snapshots"] = {name: snapshot} if snapshot is not None else None
            provider_kwargs["lazy_connect"] = lazy_connect
        provider = MCPCapabilityProvider({name: config}, **provider_kwargs)
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

    def _remove_mcp_server_registration(self, name: str, *, shutdown: bool = True) -> None:
        catalog = self._runtime.capability_catalog
        for capability_id in self._mcp_server_capability_ids.pop(name, ()):
            catalog.delete(capability_id)
        manager = self._mcp_server_managers.pop(name, None)
        if manager is not None and hasattr(manager, "shutdown"):
            catalog.remove_shutdown_hook(manager.shutdown)
            if shutdown:
                try:
                    manager.shutdown()
                except Exception:
                    pass

    def _restore_mcp_server_registration(
        self,
        name: str,
        entries: dict[str, Any],
        manager: Any | None,
    ) -> None:
        catalog = self._runtime.capability_catalog
        restored_ids = catalog.restore_entries(entries)
        if restored_ids:
            self._mcp_server_capability_ids[name] = tuple(restored_ids)
        if manager is not None:
            self._mcp_server_managers[name] = manager
            if hasattr(manager, "shutdown"):
                catalog.add_shutdown_hook(manager.shutdown)

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

    def _ensure_new_run_id_available(
        self,
        run_id: str | None,
        *,
        state: RunState | None,
    ) -> None:
        if run_id is None or state is not None:
            return
        if run_id in self._runtime.session.runs:
            raise ValueError(
                f"run_id '{run_id}' already exists; "
                "pass state to continue an existing run."
            )

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
        checkpoint: RunCheckpoint | None = None,
        graph_input: Any = None,
        review: ReviewLevel | None = None,
        dynamic_adjust: bool | None = None,
        limits: ExecutionLimits | None = None,
        execution: RunExecution = "local",
        workspace_root: str | Path = DEFAULT_RUNS_DIR,
        workspace_path: str | Path | None = None,
        run_id: str | None = None,
        input_uploads: list[ArtifactUpload] | None = None,
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        if checkpoint is not None and state is not None:
            raise TypeError("checkpoint and state cannot be supplied together.")
        continuation_checkpoint = checkpoint
        if checkpoint is not None:
            checkpoint = RunCheckpoint.model_validate(
                checkpoint.model_dump(mode="python")
            )
            continuation_checkpoint = checkpoint
            self._validate_checkpoint_runtime(checkpoint)
            state = checkpoint.state
        if run_id is not None:
            validate_run_id(run_id)
        if state is not None:
            _ensure_run_state_can_continue(state)
            if run_id is not None and run_id != state.run_id:
                raise ValueError("run_id must match state.run_id when state is supplied.")
            if continuation_checkpoint is None:
                cached = self._run_checkpoints.get(state.run_id)
                if cached is not None:
                    if cached.state != state:
                        raise ValueError(
                            "state is stale; continue from the latest checkpoint."
                        )
                    continuation_checkpoint = cached
        self._ensure_new_run_id_available(run_id, state=state)
        resolved_workspace_path = _validated_workspace_path_for_state(state, workspace_path)
        resolved_execution = _resolve_run_execution(execution, state)
        if resolved_execution == "sandbox" and resolved_workspace_path is not None:
            _ensure_sandbox_workspace_path_is_mounted(
                resolved_workspace_path,
                self._runtime.capability_catalog.workspace_root,
            )
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
        if continuation_checkpoint is not None:
            if limits is not None and limits != continuation_checkpoint.plan.limits:
                raise ValueError(
                    "limits cannot replace or expand limits restored from a checkpoint."
                )
            resolved_limits = continuation_checkpoint.plan.limits
            initial_usage = continuation_checkpoint.usage
        else:
            resolved_limits = limits or ExecutionLimits()
            initial_usage = ExecutionUsage()
        budget = ExecutionBudget(resolved_limits, initial_usage)
        with (
            self._run_scope(
                resolved_execution,
                skill_names=skill_names,
            ),
            execution_budget_scope(budget),
        ):
            return await self._run_dispatch(
                target,
                messages=messages,
                state=state,
                graph_input=graph_input,
                review=review,
                dynamic_adjust=dynamic_adjust,
                limits=resolved_limits,
                budget=budget,
                workspace_root=workspace_root,
                workspace_path=resolved_workspace_path,
                run_id=run_id,
                input_uploads=input_uploads,
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
        checkpoint: RunCheckpoint | None = None,
        graph_input: Any = None,
        review: ReviewLevel | None = None,
        dynamic_adjust: bool | None = None,
        limits: ExecutionLimits,
        budget: ExecutionBudget,
        workspace_root: str | Path = DEFAULT_RUNS_DIR,
        workspace_path: str | Path | None = None,
        run_id: str | None = None,
        input_uploads: list[ArtifactUpload] | None = None,
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        if isinstance(target, AutoAgent):
            if graph_input is not None:
                raise TypeError("graph_input is not accepted for AutoAgent targets.")
            run_messages = _require_messages(messages, "AutoAgent")
            resolved = self._runtime_for_auto_agent(target)
            review_level = review or target.review
            resolved_dynamic_adjust = (
                target.dynamic_adjust if dynamic_adjust is None else dynamic_adjust
            )
            result = await resolved.runtime.handle_messages(
                run_messages,
                run_state=state,
                mode="auto",
                review_level=review_level,
                dynamic_adjust=resolved_dynamic_adjust,
                workspace_root=self._resolve_run_workspace_root(workspace_root),
                workspace_path=workspace_path,
                run_id=run_id,
                input_uploads=input_uploads,
                capability_scope=CapabilityScope(
                    capability_ids=resolved.capability_ids,
                    skills=resolved.skill_ids,
                ),
                on_token=on_token,
                on_event=on_event,
            )
            return self._finalize_run_result(
                result,
                runtime=resolved.runtime,
                capability_ids=resolved.capability_ids,
                skill_ids=resolved.skill_ids,
                review_level=review_level,
                dynamic_adjust=resolved_dynamic_adjust,
                limits=limits,
                usage=budget.snapshot(),
            )

        if isinstance(target, ToolAgent):
            if graph_input is not None:
                raise TypeError("graph_input is not accepted for ToolAgent targets.")
            run_messages = _require_messages(messages, "ToolAgent")
            resolved = self._runtime_for_tool_agent(target)
            review_level = review or target.review
            result = await resolved.runtime.handle_messages(
                run_messages,
                run_state=state,
                mode="tool",
                review_level=review_level,
                workspace_root=self._resolve_run_workspace_root(workspace_root),
                workspace_path=workspace_path,
                run_id=run_id,
                input_uploads=input_uploads,
                capability_scope=CapabilityScope(
                    capability_ids=resolved.capability_ids,
                    skills=resolved.skill_ids,
                ),
                on_token=on_token,
                on_event=on_event,
            )
            return self._finalize_run_result(
                result,
                runtime=resolved.runtime,
                capability_ids=resolved.capability_ids,
                skill_ids=resolved.skill_ids,
                review_level=review_level,
                dynamic_adjust=True,
                limits=limits,
                usage=budget.snapshot(),
            )

        if isinstance(target, DagAgent):
            if graph_input is not None:
                raise TypeError("graph_input is not accepted for DagAgent targets.")
            run_messages = _require_messages(messages, "DagAgent")
            resolved = self._runtime_for_dag_agent(target)
            review_level = review or target.review
            resolved_dynamic_adjust = (
                target.dynamic_adjust if dynamic_adjust is None else dynamic_adjust
            )
            result = await resolved.runtime.handle_messages(
                run_messages,
                run_state=state,
                mode="dag",
                review_level=review_level,
                dynamic_adjust=resolved_dynamic_adjust,
                workspace_root=self._resolve_run_workspace_root(workspace_root),
                workspace_path=workspace_path,
                run_id=run_id,
                input_uploads=input_uploads,
                capability_scope=CapabilityScope(
                    capability_ids=resolved.capability_ids,
                    skills=resolved.skill_ids,
                ),
                on_token=on_token,
                on_event=on_event,
            )
            return self._finalize_run_result(
                result,
                runtime=resolved.runtime,
                capability_ids=resolved.capability_ids,
                skill_ids=resolved.skill_ids,
                review_level=review_level,
                dynamic_adjust=resolved_dynamic_adjust,
                limits=limits,
                usage=budget.snapshot(),
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
            result = await self._runtime.run_dag_spec(
                spec,
                graph_input=graph_input,
                workspace_root=self._resolve_run_workspace_root(workspace_root),
                workspace_path=workspace_path,
                run_id=run_id,
                artifact_uploads=artifact_uploads,
                on_token=on_token,
                on_event=on_event,
            )
            return self._finalize_static_result(
                result,
                spec=spec,
                limits=limits,
                usage=budget.snapshot(),
            )

        if isinstance(target, DAGSpec):
            if review is not None:
                raise TypeError("review is not accepted for DAGSpec targets.")
            if messages is not None:
                raise TypeError("messages is not accepted for DAGSpec targets.")
            if state is not None:
                raise TypeError("state is not accepted for DAGSpec targets.")
            spec = self._resolve_spec_capability_metadata(target)
            result = await self._runtime.run_dag_spec(
                spec,
                graph_input=graph_input,
                workspace_root=self._resolve_run_workspace_root(workspace_root),
                workspace_path=workspace_path,
                run_id=run_id,
                artifact_uploads=artifact_uploads,
                on_token=on_token,
                on_event=on_event,
            )
            return self._finalize_static_result(
                result,
                spec=spec,
                limits=limits,
                usage=budget.snapshot(),
            )

        raise TypeError("Runner.run expects an AutoAgent, ToolAgent, DagAgent, Dag, or DAGSpec target.")

    def _finalize_static_result(
        self,
        result: RunResult,
        *,
        spec: DAGSpec,
        limits: ExecutionLimits,
        usage: ExecutionUsage,
    ) -> RunResult:
        capability_ids = tuple(sorted({
            invocation.capability_id
            for invocation in iter_dag_invocations(spec.nodes)
        }))
        skill_ids = self._resolve_skill_ids(None)
        return self._finalize_run_result(
            result,
            runtime=self._runtime,
            capability_ids=capability_ids,
            skill_ids=skill_ids,
            review_level="fast",
            dynamic_adjust=True,
            limits=limits,
            usage=usage,
        )

    def _finalize_run_result(
        self,
        result: RunResult,
        *,
        runtime: HarnessRuntime,
        capability_ids: tuple[str, ...],
        skill_ids: tuple[str, ...],
        review_level: ReviewLevel,
        dynamic_adjust: bool,
        limits: ExecutionLimits,
        usage: ExecutionUsage,
    ) -> RunResult:
        capability_ids = tuple(sorted(capability_ids))
        skill_ids = tuple(sorted(skill_ids))
        state = result.state.model_copy(update={
            "review_level": review_level,
            "dynamic_adjust": dynamic_adjust,
            "capability_scope": RunCapabilityScope(
                capability_ids=capability_ids,
                skills=skill_ids,
            ),
        })
        state = self._runtime.session.save_run_state(state)
        validator_profile = (
            runtime.validator.agent.profile.model_copy(deep=True)
            if runtime.validator is not None
            else None
        )
        plan = ResolvedRunPlan(
            runtime_kind=state.kind,
            tool_profile=runtime.tool_agent.profile.model_copy(deep=True),
            planner_profile=runtime.dag_agent.profile.model_copy(deep=True),
            max_tool_steps=runtime.tool_agent.max_steps,
            max_dag_cycles=runtime.dag_agent.loop.max_cycles,
            review_level=review_level,
            dynamic_adjust=dynamic_adjust,
            capability_ids=capability_ids,
            skill_ids=skill_ids,
            agent_ids=tuple(
                capability_id
                for capability_id in capability_ids
                if capability_id.startswith("agent.")
            ),
            validation_enabled=runtime.enable_validation,
            validator_profile=validator_profile,
            max_validation_retries=runtime.max_validation_retries,
            limits=limits,
        )
        finalized = replace(result, state=state, plan=plan, usage=usage)
        checkpoint = finalized.checkpoint
        if checkpoint is None:
            raise RuntimeError("SDK run result did not produce a checkpoint.")
        self._run_checkpoints[state.run_id] = checkpoint.model_copy(deep=True)
        return finalized

    def _resolve_skill_ids(self, names: tuple[str, ...] | None) -> tuple[str, ...]:
        return tuple(sorted(
            entry.qualified_name
            for entry in visible_skills(self._skill_provider.store.list(), names)
        ))

    async def stream(
        self,
        target: RunTarget,
        *,
        messages: list[dict[str, Any]] | None = None,
        state: RunState | None = None,
        checkpoint: RunCheckpoint | None = None,
        graph_input: Any = None,
        review: ReviewLevel | None = None,
        dynamic_adjust: bool | None = None,
        limits: ExecutionLimits | None = None,
        execution: RunExecution = "local",
        workspace_root: str | Path = DEFAULT_RUNS_DIR,
        workspace_path: str | Path | None = None,
        run_id: str | None = None,
        input_uploads: list[ArtifactUpload] | None = None,
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
    ) -> AsyncIterator[RunStreamEvent]:
        """Run a target and yield typed stream events."""

        if checkpoint is not None and state is not None:
            raise TypeError("checkpoint and state cannot be supplied together.")
        continuation_state = checkpoint.state if checkpoint is not None else state
        if run_id is not None:
            validate_run_id(run_id)
        if continuation_state is not None and run_id is not None and run_id != continuation_state.run_id:
            raise ValueError("run_id must match state.run_id when state is supplied.")
        self._ensure_new_run_id_available(run_id, state=continuation_state)

        async def run_target(on_event: LoopEventHandler) -> RunResult:
            return await self.run(
                target,
                messages=messages,
                state=state,
                checkpoint=checkpoint,
                graph_input=graph_input,
                review=review,
                dynamic_adjust=dynamic_adjust,
                limits=limits,
                execution=execution,
                workspace_root=workspace_root,
                workspace_path=workspace_path,
                run_id=run_id,
                input_uploads=input_uploads,
                artifact_uploads=artifact_uploads,
                on_event=on_event,
            )

        async for event in self._stream_run(run_target):
            yield event

    async def cancel(self, run_id: str) -> bool:
        """Cancel an active streamed run and signal blocking capabilities to stop."""

        self._ensure_open()
        validate_run_id(run_id)
        task = self._active_run_tasks.get(run_id)
        event = self._active_run_cancellation_events.get(run_id)
        if task is None or event is None or task.done():
            return False
        event.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return True

    async def resume_stream(
        self,
        decision: ReviewDecision,
        *,
        checkpoint: RunCheckpoint | None = None,
        state: RunState | None = None,
        execution: RunExecution = "local",
    ) -> AsyncIterator[RunStreamEvent]:
        """Resume a pending review and yield typed stream events."""

        async def run_target(on_event: LoopEventHandler) -> RunResult:
            result = await self.resume(
                decision,
                checkpoint=checkpoint,
                state=state,
                execution=execution,
                on_event=on_event,
            )
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
        cancellation_event = threading.Event()

        def with_sequence(event: RunStreamEvent) -> RunStreamEvent:
            nonlocal sequence
            sequence += 1
            return replace(event, sequence=sequence, run_id=event.run_id or run_id)

        def emit_event(event: dict[str, Any]) -> None:
            nonlocal run_id
            stream_event = _stream_event_from_runtime(event)
            if stream_event.type == "run.started" and stream_event.run_id is not None:
                run_id = stream_event.run_id
                self._active_run_tasks[run_id] = task
                self._active_run_cancellation_events[run_id] = cancellation_event
            queue.put_nowait(with_sequence(stream_event))

        async def guarded() -> RunResult:
            try:
                with run_cancellation_context(cancellation_event):
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
                cancellation_event.set()
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if run_id is not None:
                self._active_run_tasks.pop(run_id, None)
                self._active_run_cancellation_events.pop(run_id, None)

    async def resume(
        self,
        decision: ReviewDecision,
        *,
        checkpoint: RunCheckpoint | None = None,
        state: RunState | None = None,
        execution: RunExecution = "local",
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult | None:
        if checkpoint is not None and state is not None:
            raise TypeError("checkpoint and state cannot be supplied together.")

        session_state = self._runtime.session.get_review_state(decision.review_id)
        selected_checkpoint = checkpoint
        if selected_checkpoint is None and state is not None:
            cached = self._run_checkpoints.get(state.run_id)
            if cached is not None and cached.state == state:
                selected_checkpoint = cached
        if selected_checkpoint is None and state is None and session_state is not None:
            selected_checkpoint = self._run_checkpoints.get(session_state.run_id)

        if selected_checkpoint is not None:
            return await self._resume_checkpoint(
                decision,
                selected_checkpoint,
                execution=execution,
                on_token=on_token,
                on_event=on_event,
            )

        if state is not None:
            warnings.warn(
                "Runner.resume(..., state=...) cannot restore target-specific runtime "
                "configuration; persist result.checkpoint and pass checkpoint=... instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            decision = _decision_for_resume_state(decision, state)
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

    async def _resume_checkpoint(
        self,
        decision: ReviewDecision,
        checkpoint: RunCheckpoint,
        *,
        execution: RunExecution,
        on_token: TokenHandler | None,
        on_event: LoopEventHandler | None,
    ) -> RunResult | None:
        checkpoint = RunCheckpoint.model_validate(
            checkpoint.model_dump(mode="python")
        )
        self._validate_checkpoint_runtime(checkpoint)
        decision = _decision_for_resume_state(decision, checkpoint.state)
        if decision.review_id in self._consumed_review_ids:
            raise ValueError(
                f"Checkpoint review '{decision.review_id}' has already been consumed."
            )
        review_level = decision.review_level or checkpoint.plan.review_level
        plan_payload = checkpoint.plan.model_dump(
            mode="python",
            exclude={"fingerprint"},
        )
        plan_payload["review_level"] = review_level
        plan = ResolvedRunPlan.model_validate(plan_payload)
        resume_state = checkpoint.state.model_copy(
            update={"review_level": review_level}
        )
        restored = RunCheckpoint(
            state=resume_state,
            plan=plan,
            usage=checkpoint.usage,
        )
        runtime = self._runtime_for_resolved_plan(plan)
        budget = ExecutionBudget(plan.limits, restored.usage)
        resolved_execution = _resolve_run_execution(execution, resume_state)
        self._consumed_review_ids.add(decision.review_id)
        try:
            with (
                self._run_scope(
                    resolved_execution,
                    skill_names=plan.skill_ids,
                ),
                execution_budget_scope(budget),
            ):
                result = await runtime.resume_review(
                    decision.review_id,
                    run_state=resume_state,
                    dag=decision.dag,
                    approved=decision.approved,
                    review_level=decision.review_level,
                    feedback=decision.feedback,
                    on_token=on_token,
                    on_event=on_event,
                )
                if result is None:
                    raise RuntimeError(
                        f"Checkpoint review '{decision.review_id}' could not be restored."
                    )
                return self._finalize_run_result(
                    result,
                    runtime=runtime,
                    capability_ids=plan.capability_ids,
                    skill_ids=plan.skill_ids,
                    review_level=review_level,
                    dynamic_adjust=plan.dynamic_adjust,
                    limits=plan.limits,
                    usage=budget.snapshot(),
                )
        except BaseException as exc:
            usage = budget.snapshot()
            failed_update: dict[str, Any] = {
                "status": "failed",
                "pending_review": None,
                "pending_invocation": None,
            }
            if (
                resume_state.pending_review is not None
                and resume_state.pending_review.kind == "capability_review"
                and runtime.tool_agent.messages
            ):
                failed_update["internal_messages"] = [
                    dict(message) for message in runtime.tool_agent.messages
                ]
                failed_update["trace"] = runtime.tool_agent.trace
            failed_state = resume_state.model_copy(update=failed_update)
            failed_state = self._runtime.session.save_run_state(failed_state)
            failed_checkpoint = RunCheckpoint(
                state=failed_state,
                plan=plan,
                usage=usage,
            )
            self._run_checkpoints[failed_state.run_id] = failed_checkpoint.model_copy(
                deep=True
            )
            if isinstance(exc, ExecutionLimitExceeded):
                exc.attach_checkpoint(failed_checkpoint, usage)
            raise

    def _validate_checkpoint_runtime(self, checkpoint: RunCheckpoint) -> None:
        if checkpoint.plan.runtime_kind == "static_dag":
            raise ValueError("Static DAG checkpoints do not support review resume.")
        catalog = self._runtime.capability_catalog
        for capability_id in checkpoint.plan.capability_ids:
            definition = catalog.get(capability_id)
            if definition is None:
                raise ValueError(
                    f"Checkpoint capability is not registered: {capability_id}"
                )
            if not definition.enabled:
                raise ValueError(
                    f"Checkpoint capability is disabled: {capability_id}"
                )
            if capability_id in checkpoint.plan.agent_ids and definition.kind != "agent":
                raise ValueError(
                    f"Checkpoint agent capability has incompatible kind: {capability_id}"
                )
        available_skills = {
            entry.qualified_name for entry in self._skill_provider.store.list()
        }
        missing_skills = sorted(set(checkpoint.plan.skill_ids) - available_skills)
        if missing_skills:
            raise ValueError(
                "Checkpoint skills are not available: " + ", ".join(missing_skills)
            )

    def _runtime_for_resolved_plan(self, plan: ResolvedRunPlan) -> HarnessRuntime:
        validator = (
            ValidatorAgent(
                provider=self._runtime.provider,
                profile=plan.validator_profile.model_copy(deep=True),
            )
            if plan.validator_profile is not None
            else None
        )
        runtime = _assemble_runtime(
            provider=self._runtime.provider,
            capability_executor=self._runtime.capability_executor,
            catalog=self._runtime.capability_catalog,
            tool_adapter=_tool_adapter(
                self._runtime.capability_catalog,
                plan.capability_ids,
            ),
            tool_profile=plan.tool_profile,
            tool_max_steps=plan.max_tool_steps,
            dag_profile=plan.planner_profile,
            dag_max_cycles=plan.max_dag_cycles,
            validator=validator,
            enable_validation=plan.validation_enabled,
            max_validation_retries=plan.max_validation_retries,
            profile_root=None,
        )
        runtime.session = self._runtime.session
        runtime.runs = self._runtime.runs
        return runtime

    def _runtime_for_auto_agent(self, agent: AutoAgent) -> _ResolvedRuntime:
        capability_ids, skill_ids = self._resolve_agent_scope(agent)
        runtime = _runtime_from_existing(
            self._runtime,
            tool_profile=agent.profile,
            tool_max_steps=agent.max_steps,
            dag_profile=agent.planner_profile,
            dag_max_cycles=agent.max_cycles,
            visible_capability_ids=capability_ids,
            profile_root=self.profile_root,
        )
        return _ResolvedRuntime(runtime, capability_ids, skill_ids)

    def _runtime_for_tool_agent(self, agent: ToolAgent) -> _ResolvedRuntime:
        capability_ids, skill_ids = self._resolve_agent_scope(agent)
        runtime = _runtime_from_existing(
            self._runtime,
            tool_profile=agent.profile,
            tool_max_steps=agent.max_steps,
            dag_profile="dag_agent",
            dag_max_cycles=6,
            visible_capability_ids=capability_ids,
            profile_root=self.profile_root,
        )
        return _ResolvedRuntime(runtime, capability_ids, skill_ids)

    def _runtime_for_dag_agent(self, agent: DagAgent) -> _ResolvedRuntime:
        capability_ids, skill_ids = self._resolve_agent_scope(agent)
        runtime = _runtime_from_existing(
            self._runtime,
            tool_profile="conversation",
            tool_max_steps=8,
            dag_profile=agent.planner_profile,
            dag_max_cycles=agent.max_cycles,
            visible_capability_ids=capability_ids,
            profile_root=self.profile_root,
        )
        return _ResolvedRuntime(runtime, capability_ids, skill_ids)

    def _resolve_agent_scope(
        self,
        agent: AutoAgent | ToolAgent | DagAgent,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        requested_skills = _agent_skills(agent)
        skill_ids = self._resolve_skill_ids(requested_skills)
        capability_ids = self._resolve_agent_capability_refs(
            agent.capabilities,
            skill_ids,
            agents=agent.agents,
        )
        enabled_ids = tuple(
            capability_id
            for capability_id in capability_ids
            if (
                (definition := self._runtime.capability_catalog.get(capability_id))
                is not None
                and definition.enabled
            )
        )
        return enabled_ids, skill_ids

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

    def _validate_agent_registration(self, agent: ToolAgent, *, replacing: bool) -> None:
        name = validate_agent_name(agent.name)
        if agent.review != "fast":
            raise ValueError("Registered subagents must use review=\"fast\".")
        capability_id = f"agent.{name}"
        existing = self._registered_agent_configs.get(name)
        if existing is not None and not replacing and existing != agent:
            raise ValueError(f"Agent capability '{capability_id}' is already registered with different config.")
        self._runtime.capability_catalog.validate_registerable(
            CapabilityDefinition(id=capability_id, kind="agent"),
            ignore_ids=(capability_id,) if existing is not None else (),
        )
        self._registered_agent_runtime_config(agent, register_bindings=False)

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

    def _registered_agent_runtime_config(
        self,
        agent: ToolAgent,
        *,
        register_bindings: bool = True,
    ) -> dict[str, Any]:
        name = validate_agent_name(agent.name)
        if _has_agent_refs(agent.agents):
            raise ValueError(f"Registered subagent 'agent.{name}' cannot expose subagents.")
        capability_ids = self._resolve_agent_capability_refs(
            agent.capabilities,
            agent.skills,
            register_bindings=register_bindings,
        )
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
            "Run state is awaiting review; use Runner.resume(..., checkpoint=...) "
            "with the SDK-produced RunCheckpoint."
        )


def _validated_workspace_path_for_state(
    state: RunState | None,
    workspace_path: str | Path | None,
) -> Path | None:
    if workspace_path is None:
        return None
    resolved = Path(workspace_path).expanduser().resolve()
    if state is None or not state.workspace_path:
        return resolved
    state_path = Path(state.workspace_path).expanduser().resolve()
    if state_path != resolved:
        raise ValueError(
            f"workspace_path '{resolved}' does not match run state workspace_path '{state_path}'."
        )
    return resolved


def _ensure_sandbox_workspace_path_is_mounted(workspace_path: Path, workspace_root: Path) -> None:
    root = Path(workspace_root).expanduser().resolve()
    try:
        workspace_path.relative_to(root)
    except ValueError as exc:
        raise SandboxExecutionError(
            f"workspace_path '{workspace_path}' is outside sandbox workspace root '{root}'."
        ) from exc


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
    issues: list[ValidationIssue] = []
    for item in value or []:
        if isinstance(item, ValidationIssue):
            issues.append(item)
            continue
        if isinstance(item, dict):
            issues.append(ValidationIssue.model_validate(item))
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
) -> bool:
    definition, handler, supports_context = _capability_parts(capability)
    existing = catalog.get_entry(definition.id)
    expected_handler = expected_handler or handler
    expected_supports_context = (
        supports_context if expected_supports_context is None else expected_supports_context
    )
    if existing is not None:
        if _entry_matches_binding(existing, definition, expected_handler, expected_supports_context):
            return False
        raise ValueError(f"Capability '{definition.id}' is already registered with different config.")
    catalog.register(definition, handler, supports_context=supports_context)
    return True


def _validate_capability_binding_batch(
    catalog,
    capabilities: Iterable[CapabilityBinding],
    *,
    ignore_ids: Iterable[str] = (),
) -> None:
    ignored = set(ignore_ids)
    seen_ids: set[str] = set()
    seen_names: dict[str, str] = {}
    for capability in capabilities:
        definition, handler, supports_context = _capability_parts(capability)
        if definition.id in seen_ids:
            raise ValueError(f"Capability '{definition.id}' is already registered.")
        existing_seen_id = seen_names.get(definition.name)
        if existing_seen_id is not None:
            raise ValueError(
                f"Capability name '{definition.name}' is already registered by '{existing_seen_id}'."
            )
        seen_ids.add(definition.id)
        seen_names[definition.name] = definition.id
        existing = catalog.get_entry(definition.id)
        if definition.id not in ignored and _entry_matches_binding(
            existing,
            definition,
            handler,
            supports_context,
        ):
            continue
        if existing is not None and definition.id not in ignored:
            raise ValueError(f"Capability '{definition.id}' is already registered with different config.")
        catalog.validate_registerable(definition, ignore_ids=ignored)


def _entry_matches_binding(
    entry,
    definition: CapabilityDefinition,
    handler: CapabilityHandler,
    supports_context: bool,
) -> bool:
    return (
        entry is not None
        and entry.definition == definition
        and entry.handler is handler
        and entry.supports_context == supports_context
    )


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
