"""Factories for the real MiniMax/OpenAI-compatible harness stack."""

from __future__ import annotations

from pathlib import Path

from dagent.capabilities import CapabilityToolAdapter, CapabilityToolset, create_default_capability_catalog
from dagent.capabilities.providers import AgentCapabilityProvider
from dagent.config import DagentConfig, load_config
from dagent.harness_runtime import (
    ToolAgent,
    ToolAgentLoop,
    DAGAgent,
    DAGAgentLoop,
    DAGExecutor,
    ValidatorAgent,
    FeedbackLearnerAgent,
    HarnessRuntime,
)
from dagent.harness_runtime import CapabilityExecutor
from dagent.profiles import ProfileStore
from dagent.providers import OpenAICompatibleProvider


def create_harness_runtime(
    *,
    config: DagentConfig | None = None,
    workspace_root: str | Path = ".",
) -> HarnessRuntime:
    resolved_config = config or load_config()
    profile_store = ProfileStore(resolved_config.profiles.directory)
    provider = OpenAICompatibleProvider(resolved_config.provider)
    session_capabilities = create_default_capability_catalog(workspace_root=workspace_root)
    capability_executor = CapabilityExecutor(session_capabilities)
    profile_agent_configs = _profile_agent_configs(
        profile_store=profile_store,
        config=resolved_config,
        provider=provider,
        capability_executor=capability_executor,
    )
    if profile_agent_configs:
        AgentCapabilityProvider(profile_agent_configs).register_into(session_capabilities)
    tool_adapter = CapabilityToolAdapter(
        session_capabilities,
        toolsets=[CapabilityToolset("builtin", tuple(sorted(session_capabilities.ids())))],
    )
    for agent_config in profile_agent_configs.values():
        agent_config["tool_adapter"] = tool_adapter
    conversation_profile = profile_store.load(resolved_config.profiles.conversation)
    tool_agent_loop = ToolAgentLoop(
        provider=provider,
        capability_executor=capability_executor,
        tool_adapter=tool_adapter,
    )
    tool_agent = ToolAgent(
        loop=tool_agent_loop,
        profile=conversation_profile,
    )
    dag_executor = DAGExecutor(capability_executor=capability_executor)
    dag_agent_loop = DAGAgentLoop(
        provider=provider,
        dag_executor=dag_executor,
        tool_adapter=tool_adapter,
    )
    dag_agent = DAGAgent(
        loop=dag_agent_loop,
        profile=profile_store.load(resolved_config.profiles.dag_agent),
    )
    validator = _try_load_validator(provider, profile_store, resolved_config)
    return HarnessRuntime(
        provider=provider,
        tool_agent=tool_agent,
        dag_agent=dag_agent,
        validator=validator,
        enable_validation=resolved_config.enable_result_validation,
        capability_catalog=session_capabilities,
        capability_executor=capability_executor,
    )


def create_profile_agents(
    *,
    config: DagentConfig | None = None,
) -> tuple[ValidatorAgent, FeedbackLearnerAgent]:
    resolved_config = config or load_config()
    provider = OpenAICompatibleProvider(resolved_config.provider)
    profile_store = ProfileStore(resolved_config.profiles.directory)
    return (
        ValidatorAgent(
            provider=provider,
            profile=profile_store.load(resolved_config.profiles.validator_agent),
        ),
        FeedbackLearnerAgent(
            provider=provider,
            profile=profile_store.load(resolved_config.profiles.feedback_learner),
        ),
    )


def _profile_agent_configs(
    *,
    profile_store: ProfileStore,
    config: DagentConfig,
    provider: OpenAICompatibleProvider,
    capability_executor: CapabilityExecutor,
) -> dict[str, dict[str, object]]:
    profile_root = Path(config.profiles.directory)
    excluded = {
        config.profiles.dag_agent,
        config.profiles.validator_agent,
        config.profiles.feedback_learner,
    }
    if not profile_root.exists():
        return {}

    agents: dict[str, dict[str, object]] = {}
    for profile_dir in sorted((path for path in profile_root.iterdir() if path.is_dir()), key=lambda path: path.name):
        if profile_dir.name in excluded or not (profile_dir / "profile.yaml").exists():
            continue
        try:
            profile = profile_store.load(profile_dir.name)
        except Exception:
            continue
        agents[profile.name] = {
            "provider": provider,
            "profile": profile,
            "description": profile.description or profile.role,
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Instruction for this agent when no DAG node context is available.",
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "Maximum tool-use steps for the agent node.",
                        "default": 8,
                    },
                },
            },
            "risk": "medium",
            "capability_executor": capability_executor,
        }
    return agents


def _try_load_validator(
    provider: OpenAICompatibleProvider,
    profile_store: ProfileStore,
    config: DagentConfig,
) -> ValidatorAgent | None:
    try:
        profile = profile_store.load(config.profiles.validator_agent)
    except Exception:
        return None
    return ValidatorAgent(provider=provider, profile=profile)
