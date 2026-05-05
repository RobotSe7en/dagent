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
    def __init__(self) -> None:
        self.calls = 0

    async def run(
        self,
        user_message: str,
        *,
        boundary: Boundary,
        max_steps: int = 8,
        allowed_tools: list[str] | None = None,
        messages: list[dict] | None = None,
    ) -> AgentLoopResult:
        self.calls += 1
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
    provider = MockProvider([ChatResponse(content="hello")])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("hello", mode="auto"))

    assert result.status == "completed"
    assert result.message_markdown == "hello"
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
            ChatResponse(content=_dag_creator_json(tools=["write_file"])),
        ]
    )
    runtime = _runtime(provider)

    result = run(runtime.handle_message("Do a complex risky task", mode="auto"))

    assert result.status == "awaiting_dag_review"
    assert result.dag is not None
    assert result.dag.status == "review_required"
    assert result.dag.nodes[0].risk == "medium"
    assert result.task_id in runtime.tasks


def test_harness_runtime_dag_creator_does_not_auto_execute_approved_dag() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="dag_creator",
                        arguments={
                            "request": "Create a safe DAG.",
                            "reason": "Needs reviewable orchestration.",
                        },
                    )
                ]
            ),
            ChatResponse(content=_dag_creator_json()),
        ]
    )
    node_loop = CompletingLoop()
    runtime = _runtime(provider, node_loop=node_loop)

    result = run(runtime.handle_message("Create a safe DAG", mode="auto"))

    assert result.status == "awaiting_dag_review"
    assert result.dag is not None
    assert result.dag.status == "approved"
    assert node_loop.calls == 0


def test_harness_runtime_resume_dag_returns_top_agent_summary() -> None:
    final_answer = "final answer: echo:ok"
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="dag_creator",
                        arguments={
                            "request": "Create a safe DAG.",
                            "reason": "Needs execution.",
                        },
                    )
                ]
            ),
            ChatResponse(content=_dag_creator_json()),
            ChatResponse(content=final_answer),
        ]
    )
    node_loop = CompletingLoop()
    runtime = _runtime(provider, node_loop=node_loop)

    result = run(runtime.handle_message("What files are here?", mode="auto"))
    resumed = run(runtime.resume_dag(result.task_id, result.dag))

    assert result.status == "awaiting_dag_review"
    assert resumed.status == "completed"
    assert resumed.run_result is not None
    assert resumed.run_result.completed is True
    assert resumed.message_markdown == final_answer
    assert node_loop.calls == 0
    summary_requests = [
        request
        for request in provider.requests
        if any("DAG execution observation" in message.get("content", "") for message in request["messages"])
    ]
    assert summary_requests
    summary_content = "\n".join(message.get("content", "") for message in summary_requests[-1]["messages"])
    assert "Summarize the DAG execution result" in summary_content
    assert "echo:ok" in summary_content


def test_harness_runtime_dag_mode_forces_reviewable_dag_without_top_tool_call() -> None:
    provider = MockProvider([ChatResponse(content=_dag_creator_json())])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("Create a safe DAG", mode="dag"))

    assert result.status == "awaiting_dag_review"
    assert result.dag is not None
    assert result.dag.status == "approved"
    assert result.task_id in runtime.tasks
    assert len(provider.requests) == 1


def _runtime(
    provider: MockProvider,
    *,
    node_loop: CompletingLoop | None = None,
    auto_execute_approved_dags: bool = False,
) -> HarnessRuntime:
    tool_executor = make_tool_executor()
    agent_loop = AgentLoop(provider=provider, tool_executor=tool_executor)
    dag_creator = LLMDagCreator(provider, profile=_dag_creator_profile())
    return HarnessRuntime(
        agent_loop=agent_loop,
        dag_creator=dag_creator,
        dag_executor=DAGExecutor(
            agent_loop=node_loop or CompletingLoop(),
            tool_executor=tool_executor,
        ),
        conversation_profile=_conversation_profile(),
        runtime_tools=[],
        auto_execute_approved_dags=auto_execute_approved_dags,
    )


def make_tool_executor() -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(
        name="echo",
        handler=lambda text: f"echo:{text}",
        action="read",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    registry.register(
        name="write_file",
        handler=lambda path, content="": f"wrote:{path}:{content}",
        action="write",
        path_args=("path",),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path"],
        },
    )
    return ToolExecutor(registry)


def _conversation_profile() -> AgentProfile:
    return AgentProfile(
        name="conversation",
        role="conversation",
        layers=["soul"],
        layer_contents={"soul": "You are a conversation agent."},
    )


def _dag_creator_profile() -> AgentProfile:
    return AgentProfile(
        name="dag_creator",
        role="dag_creator",
        layers=["soul"],
        layer_contents={"soul": "You are a DAG creator."},
    )


def _dag_creator_json(*, tools: list[str] | None = None) -> str:
    tool = (tools or ["echo"])[0]
    args = {"path": "notes.md", "content": "hi"} if tool == "write_file" else {"text": "ok"}
    boundary = {
        "mode": "write_limited" if tool == "write_file" else "read_only",
        "allowed_paths": ["notes.md"] if tool == "write_file" else [],
        "forbidden_tools": [],
        "allowed_commands": [],
        "forbidden_commands": [],
    }
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
                    "kind": "tool",
                    "tool": tool,
                    "args": args,
                    "agent": None,
                    "tools": [tool],
                    "skills": [],
                    "boundary": boundary,
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
