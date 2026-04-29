"""Factories for the real MiniMax/OpenAI-compatible harness stack."""

from __future__ import annotations

from pathlib import Path

from dagent.config import DagentConfig, load_config
from dagent.harness import DAGExecutor
from dagent.harness.control_plane import ControlPlane
from dagent.harness.dag_review import DAGReviewerAgent
from dagent.harness.feedback_learner import FeedbackLearnerAgent
from dagent.harness.planner import LLMPlanner
from dagent.profiles import ProfileStore
from dagent.providers import OpenAICompatibleProvider
from dagent.runtime import AgentLoop
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
    dag_executor = DAGExecutor(agent_loop=agent_loop)
    planner = LLMPlanner(
        provider,
        profile_store=profile_store,
        profile_name=resolved_config.profiles.planner,
    )
    return ControlPlane(planner=planner, executor=dag_executor)


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
