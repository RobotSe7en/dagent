"""Factories for the real MiniMax/OpenAI-compatible harness stack."""

from __future__ import annotations

from pathlib import Path

from dagent.config import DagentConfig, load_config
from dagent.harness_runtime import (
    AgentLoop,
    ControlPlane,
    DAGExecutor,
    DAGReviewerAgent,
    FeedbackLearnerAgent,
    HarnessRuntime,
    LLMDagCreator,
)
from dagent.profiles import ProfileStore
from dagent.providers import OpenAICompatibleProvider
from dagent.tools.executor import ToolExecutor
from dagent.tools.file_tools import create_file_tool_registry


def create_control_plane(
    *,
    config: DagentConfig | None = None,
    workspace_root: str | Path = ".",
) -> ControlPlane:
    resolved_config = config or load_config()
    profile_store = ProfileStore(resolved_config.profiles.directory)
    provider = OpenAICompatibleProvider(resolved_config.provider)
    tool_executor = ToolExecutor(
        create_file_tool_registry(),
        workspace_root=workspace_root,
    )
    agent_loop = AgentLoop(provider=provider, tool_executor=tool_executor)
    dag_executor = DAGExecutor(agent_loop=agent_loop, tool_executor=tool_executor)
    dag_creator = LLMDagCreator(
        provider,
        profile_store=profile_store,
        profile_name=resolved_config.profiles.dag_creator,
        tools=[
            tool
            for name in sorted(tool_executor.registry.names())
            if (tool := tool_executor.registry.get(name)) is not None
        ],
    )
    return ControlPlane(dag_creator=dag_creator, executor=dag_executor)


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
    dag_executor = DAGExecutor(agent_loop=agent_loop, tool_executor=tool_executor)
    dag_creator = LLMDagCreator(
        provider,
        profile_store=profile_store,
        profile_name=resolved_config.profiles.dag_creator,
        tools=runtime_tools,
    )
    return HarnessRuntime(
        agent_loop=agent_loop,
        dag_creator=dag_creator,
        dag_executor=dag_executor,
        conversation_profile=profile_store.load(resolved_config.profiles.conversation),
        runtime_tools=runtime_tools,
    )


def create_profile_agents(
    *,
    config: DagentConfig | None = None,
) -> tuple[DAGReviewerAgent, FeedbackLearnerAgent]:
    resolved_config = config or load_config()
    provider = OpenAICompatibleProvider(resolved_config.provider)
    profile_store = ProfileStore(resolved_config.profiles.directory)
    return (
        DAGReviewerAgent(
            provider=provider,
            profile=profile_store.load(resolved_config.profiles.dag_reviewer),
        ),
        FeedbackLearnerAgent(
            provider=provider,
            profile=profile_store.load(resolved_config.profiles.feedback_learner),
        ),
    )
