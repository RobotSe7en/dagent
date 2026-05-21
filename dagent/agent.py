"""Public agent SDK entrypoints."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from dagent.capabilities import CapabilityToolAdapter, CapabilityToolset, create_default_capability_catalog
from dagent.capabilities.catalog import CapabilityHandler
from dagent.capabilities.decorator import CapabilityBinding
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
from dagent.harness_runtime.review_policy import ReviewLevel
from dagent.harness_runtime.tool_agent import LoopEventHandler, TokenHandler
from dagent.profiles import AgentProfile, ProfileStore
from dagent.providers import ChatProvider, OpenAICompatibleProvider
from dagent.result import RunResult
from dagent.review import ReviewDecision
from dagent.schemas import CapabilityDefinition, DAGRun, DAGSpec


CapabilityLike = CapabilityBinding


class ToolAgent:
    """Profile-backed tool-loop agent."""

    def __init__(
        self,
        *,
        profile: str | AgentProfile = "conversation",
        capabilities: Iterable[CapabilityLike] = (),
        workspace: str | Path = ".",
        provider: ChatProvider | None = None,
        review: ReviewLevel = "fast",
        validator: str | AgentProfile | ValidatorAgent | None = None,
        max_steps: int = 8,
    ) -> None:
        self.review = review
        self._runtime = _create_runtime(
            workspace=workspace,
            provider=provider,
            capabilities=capabilities,
            tool_profile=profile,
            validator=validator,
            tool_max_steps=max_steps,
        )

    @property
    def runtime(self) -> HarnessRuntime:
        return self._runtime

    async def run(
        self,
        input: str,
        *,
        review: ReviewLevel | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        response = await self._runtime.handle_message(
            input,
            mode="tool",
            review_level=review or self.review,
            on_token=on_token,
            on_event=on_event,
        )
        return RunResult(response)

    async def resume(
        self,
        decision: ReviewDecision,
        *,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult | None:
        response = await self._runtime.resume_review(
            decision.review_id,
            dag=decision.dag,
            approved=decision.approved,
            review_level=decision.review_level,
            on_token=on_token,
            on_event=on_event,
        )
        return RunResult(response) if response is not None else None

    def add_capability(self, capability: CapabilityLike) -> CapabilityDefinition:
        return _register_capability(self._runtime, capability)

    def with_capabilities(self, capabilities: Iterable[CapabilityLike]) -> "ToolAgent":
        for capability in capabilities:
            self.add_capability(capability)
        return self

    def list_capabilities(self) -> list[CapabilityDefinition]:
        return self._runtime.capability_catalog.list(enabled_only=True)


class DagAgent:
    """Dynamic DAG planner and executor."""

    def __init__(
        self,
        *,
        capabilities: Iterable[CapabilityLike] = (),
        workspace: str | Path = ".",
        provider: ChatProvider | None = None,
        review: ReviewLevel = "fast",
        validator: str | AgentProfile | ValidatorAgent | None = None,
        max_cycles: int = 6,
    ) -> None:
        self.review = review
        self._runtime = _create_runtime(
            workspace=workspace,
            provider=provider,
            capabilities=capabilities,
            validator=validator,
            dag_max_cycles=max_cycles,
        )

    @property
    def runtime(self) -> HarnessRuntime:
        return self._runtime

    async def run(
        self,
        input: str,
        *,
        review: ReviewLevel | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        response = await self._runtime.handle_message(
            input,
            mode="dag",
            review_level=review or self.review,
            on_token=on_token,
            on_event=on_event,
        )
        return RunResult(response)

    async def resume(
        self,
        decision: ReviewDecision,
        *,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult | None:
        response = await self._runtime.resume_review(
            decision.review_id,
            dag=decision.dag,
            approved=decision.approved,
            review_level=decision.review_level,
            on_token=on_token,
            on_event=on_event,
        )
        return RunResult(response) if response is not None else None

    async def run_spec(
        self,
        spec: DAGSpec,
        *,
        workspace_root: str | Path = ".dagent-runs",
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> DAGRun:
        return await self._runtime.run_dag_spec(
            spec,
            workspace_root=workspace_root,
            artifact_uploads=artifact_uploads,
            on_token=on_token,
            on_event=on_event,
        )

    def add_capability(self, capability: CapabilityLike) -> CapabilityDefinition:
        return _register_capability(self._runtime, capability)

    def with_capabilities(self, capabilities: Iterable[CapabilityLike]) -> "DagAgent":
        for capability in capabilities:
            self.add_capability(capability)
        return self

    def list_capabilities(self) -> list[CapabilityDefinition]:
        return self._runtime.capability_catalog.list(enabled_only=True)


def create_default_runtime(
    *,
    workspace: str | Path = ".",
    provider: ChatProvider | None = None,
    capabilities: Iterable[CapabilityLike] = (),
) -> HarnessRuntime:
    """Create the default harness runtime used by the API server."""

    return _create_runtime(
        workspace=workspace,
        provider=provider,
        capabilities=capabilities,
    )


def _create_runtime(
    *,
    workspace: str | Path,
    provider: ChatProvider | None,
    capabilities: Iterable[CapabilityLike],
    tool_profile: str | AgentProfile = "conversation",
    validator: str | AgentProfile | ValidatorAgent | None = None,
    tool_max_steps: int = 8,
    dag_max_cycles: int = 6,
) -> HarnessRuntime:
    workspace_path = Path(workspace)
    resolved_provider = provider or OpenAICompatibleProvider(load_config().provider)
    catalog = create_default_capability_catalog(workspace_root=workspace_path)
    capability_executor = CapabilityExecutor(catalog)
    for capability in capabilities:
        _register_capability_parts(catalog, capability)

    tool_adapter = CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset("builtin", tuple(sorted(catalog.ids())))],
    )
    capability_loop = ToolAgentLoop(
        provider=resolved_provider,
        capability_executor=capability_executor,
        tool_adapter=tool_adapter,
    )
    tool_agent = RuntimeToolAgent(
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
    dag_agent = RuntimeDAGAgent(
        loop=dag_loop,
        profile=_resolve_profile("dag_agent", workspace_path=workspace_path, default_name="dag_agent"),
    )
    resolved_validator = _resolve_validator(validator, resolved_provider, workspace_path=workspace_path)
    return HarnessRuntime(
        provider=resolved_provider,
        tool_agent=tool_agent,
        dag_agent=dag_agent,
        validator=resolved_validator,
        enable_validation=resolved_validator is not None,
        capability_catalog=catalog,
        capability_executor=capability_executor,
    )


def _register_capability(runtime: HarnessRuntime, capability: CapabilityLike) -> CapabilityDefinition:
    definition, handler, supports_context = _capability_parts(capability)
    runtime.capability_catalog.register(definition, handler, supports_context=supports_context)
    runtime.refresh_toolsets()
    return runtime.capability_catalog.get(definition.id) or definition


def _register_capability_parts(catalog, capability: CapabilityLike) -> None:
    definition, handler, supports_context = _capability_parts(capability)
    catalog.register(definition, handler, supports_context=supports_context)


def _capability_parts(capability: CapabilityLike) -> tuple[CapabilityDefinition, CapabilityHandler, bool]:
    if not isinstance(capability, CapabilityBinding):
        raise TypeError("Expected a capability created with @dagent.tool or @dagent.capability.")
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
