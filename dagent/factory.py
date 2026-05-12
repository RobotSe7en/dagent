"""Factories for the real MiniMax/OpenAI-compatible harness stack."""

from __future__ import annotations

from pathlib import Path

from dagent.config import DagentConfig, load_config
from dagent.harness_runtime import (
    AgentLoop,
    DAGAgentLoop,
    DAGExecutor,
    ResultReviewerAgent,
    FeedbackLearnerAgent,
    HarnessRuntime,
)
from dagent.profiles import ProfileStore
from dagent.providers import OpenAICompatibleProvider
from dagent.tools.executor import ToolExecutor
from dagent.tools.file_tools import create_file_tool_registry


def create_harness_runtime(
    *,
    config: DagentConfig | None = None,
    workspace_root: str | Path = ".",
) -> HarnessRuntime:
    resolved_config = config or load_config()
    profile_store = ProfileStore(resolved_config.profiles.directory)
    provider = OpenAICompatibleProvider(resolved_config.provider)
    tool_executor = ToolExecutor(
        create_file_tool_registry(),
        workspace_root=workspace_root,
    )
    runtime_tools = [
        tool
        for name in sorted(tool_executor.registry.names())
        if (tool := tool_executor.registry.get(name)) is not None
    ]
    agent_loop = AgentLoop(provider=provider, tool_executor=tool_executor)
    dag_executor = DAGExecutor(tool_executor=tool_executor)
    dag_agent_loop = DAGAgentLoop(
        provider,
        dag_executor=dag_executor,
        profile_store=profile_store,
        profile_name=resolved_config.profiles.dag_agent,
        tools=runtime_tools,
    )
    reviewer = _try_load_reviewer(provider, profile_store, resolved_config)
    return HarnessRuntime(
        provider=provider,
        agent_loop=agent_loop,
        dag_agent_loop=dag_agent_loop,
        conversation_profile=profile_store.load(resolved_config.profiles.conversation),
        runtime_tools=runtime_tools,
        reviewer=reviewer,
        enable_reviewer=resolved_config.enable_result_reviewer,
    )


def create_profile_agents(
    *,
    config: DagentConfig | None = None,
) -> tuple[ResultReviewerAgent, FeedbackLearnerAgent]:
    resolved_config = config or load_config()
    provider = OpenAICompatibleProvider(resolved_config.provider)
    profile_store = ProfileStore(resolved_config.profiles.directory)
    return (
        ResultReviewerAgent(
            provider=provider,
            profile=profile_store.load(resolved_config.profiles.result_reviewer),
        ),
        FeedbackLearnerAgent(
            provider=provider,
            profile=profile_store.load(resolved_config.profiles.feedback_learner),
        ),
    )


def _try_load_reviewer(
    provider: OpenAICompatibleProvider,
    profile_store: ProfileStore,
    config: DagentConfig,
) -> ResultReviewerAgent | None:
    try:
        profile = profile_store.load(config.profiles.result_reviewer)
    except Exception:
        return None
    return ResultReviewerAgent(provider=provider, profile=profile)
