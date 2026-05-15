"""Factories for the real MiniMax/OpenAI-compatible harness stack."""

from __future__ import annotations

from pathlib import Path

from dagent.capabilities import RunnableRegistry
from dagent.capabilities.providers import ToolRunnableProvider
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
from dagent.harness_runtime import RunnableExecutor
from dagent.profiles import ProfileStore
from dagent.providers import OpenAICompatibleProvider
from dagent.tools.file_tools import create_file_tool_registry


def create_harness_runtime(
    *,
    config: DagentConfig | None = None,
    workspace_root: str | Path = ".",
) -> HarnessRuntime:
    resolved_config = config or load_config()
    profile_store = ProfileStore(resolved_config.profiles.directory)
    provider = OpenAICompatibleProvider(resolved_config.provider)
    runnable_registry = RunnableRegistry()
    runnable_executor = RunnableExecutor(runnable_registry, workspace_root=workspace_root)
    ToolRunnableProvider(create_file_tool_registry()).register_into(runnable_registry, runnable_executor)
    runtime_tools = runnable_registry.list(kind="tool", enabled_only=True)
    conversation_profile = profile_store.load(resolved_config.profiles.conversation)
    tool_agent_loop = ToolAgentLoop(provider=provider, runnable_executor=runnable_executor)
    tool_agent = ToolAgent(
        loop=tool_agent_loop,
        profile=conversation_profile,
        tools=runtime_tools,
    )
    dag_executor = DAGExecutor(runnable_executor=runnable_executor)
    dag_agent_loop = DAGAgentLoop(
        provider=provider,
        dag_executor=dag_executor,
    )
    dag_agent = DAGAgent(
        loop=dag_agent_loop,
        profile=profile_store.load(resolved_config.profiles.dag_agent),
        tools=runtime_tools,
    )
    validator = _try_load_validator(provider, profile_store, resolved_config)
    return HarnessRuntime(
        provider=provider,
        tool_agent=tool_agent,
        dag_agent=dag_agent,
        validator=validator,
        enable_validation=resolved_config.enable_result_validation,
        runnable_registry=runnable_registry,
        runnable_executor=runnable_executor,
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
