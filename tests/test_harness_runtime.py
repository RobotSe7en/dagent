import asyncio
import json

from dagent.harness_runtime import (
    AgentLoop,
    AgentLoopResult,
    DAGExecutor,
    HarnessRuntime,
    LLMDagCreator,
)
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import Boundary
from dagent.tools.executor import ToolExecutor
from dagent.tools.registry import ToolRegistry


class CompletingLoop:
    async def run(
        self,
        user_message: str,
        *,
        boundary: Boundary,
        max_steps: int = 8,
        allowed_tools: list[str] | None = None,
        messages: list[dict] | None = None,
    ) -> AgentLoopResult:
        return AgentLoopResult(
            final_response="node complete",
            messages=[],
            steps=1,
            completed=True,
            stop_reason="completed",
        )


def run(coro):
    return asyncio.run(coro)


def test_harness_runtime_direct_message_does_not_create_dag() -> None:
    provider = MockProvider([ChatResponse(content="你好！")])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("你好", mode="auto"))

    assert result.status == "completed"
    assert result.message_markdown == "你好！"
    assert result.dag is None
    assert runtime.tasks == {}


def test_harness_runtime_dag_creator_creates_reviewable_dag() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="dag_creator",
                        arguments={
                            "request": "Create a DAG for a complex task.",
                            "reason": "Needs reviewable orchestration.",
                        },
                    )
                ]
            ),
            ChatResponse(content=_planner_json(tools=["write_file"])),
        ]
    )
    runtime = _runtime(provider)

    result = run(runtime.handle_message("Do a complex risky task", mode="auto"))

    assert result.status == "awaiting_approval"
    assert result.dag is not None
    assert result.dag.status == "review_required"
    assert result.dag.nodes[0].risk == "medium"
    assert result.task_id in runtime.tasks


def _runtime(provider: MockProvider) -> HarnessRuntime:
    tool_executor = ToolExecutor(ToolRegistry())
    agent_loop = AgentLoop(provider=provider, tool_executor=tool_executor)
    dag_creator = LLMDagCreator(provider, profile=_planner_profile())
    return HarnessRuntime(
        agent_loop=agent_loop,
        planner=dag_creator,
        dag_executor=DAGExecutor(agent_loop=CompletingLoop()),
        conversation_profile=_conversation_profile(),
        runtime_tools=[],
    )


def _conversation_profile() -> AgentProfile:
    return AgentProfile(
        name="conversation",
        role="conversation",
        layers=["soul"],
        layer_contents={"soul": "You are a conversation agent."},
    )


def _planner_profile() -> AgentProfile:
    return AgentProfile(
        name="dag_creator",
        role="dag_creator",
        layers=["soul"],
        layer_contents={"soul": "You are a DAG planner."},
    )


def _planner_json(*, tools: list[str] | None = None) -> str:
    return json.dumps(
        {
            "dag_id": "dag_runtime",
            "task_id": "ignored",
            "version": 1,
            "status": "draft",
            "nodes": [
                {
                    "id": "node_1",
                    "title": "Node 1",
                    "goal": "Do work.",
                    "agent": None,
                    "tools": tools or [],
                    "skills": [],
                    "boundary": {
                        "mode": "read_only",
                        "allowed_paths": [],
                        "forbidden_tools": [],
                        "allowed_commands": [],
                        "forbidden_commands": [],
                    },
                    "risk": "low",
                    "risk_reason": "DagCreator estimate.",
                    "expected_output": "Result.",
                    "max_steps": 1,
                    "timeout_seconds": 30,
                }
            ],
            "edges": [],
        }
    )
