import asyncio
import json

from dagent.harness_runtime import (
    AgentLoop,
    AgentLoopResult,
    DAGExecutor,
    HarnessRuntime,
    LLMDAGAgent,
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


def test_harness_runtime_injects_registry_tools_into_dag_agent() -> None:
    provider = MockProvider([ChatResponse(content="unused")])
    runtime = _runtime(provider)

    tool_names = {tool.name for tool in runtime.dag_agent.tools}
    assert tool_names == {"dag_start", "echo", "write_file"}


def test_harness_runtime_direct_message_does_not_create_dag() -> None:
    provider = MockProvider([ChatResponse(content="hello")])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("hello", mode="auto"))

    assert result.status == "completed"
    assert result.message_markdown == "hello"
    assert result.dag is None
    assert runtime.tasks == {}


def test_harness_runtime_dag_agent_creates_reviewable_dag() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="dag_agent",
                        arguments={
                            "request": "Create a DAG for a complex task.",
                            "reason": "Needs reviewable orchestration.",
                        },
                    )
                ]
            ),
            ChatResponse(content=_dag_agent_json(tools=["write_file"])),
        ]
    )
    runtime = _runtime(provider)

    result = run(runtime.handle_message("Do a complex risky task", mode="auto"))

    assert result.status == "awaiting_dag_review"
    assert result.dag is not None
    assert result.dag.status == "review_required"
    assert result.dag.nodes[0].risk == "medium"
    assert result.task_id in runtime.tasks


def test_harness_runtime_dag_agent_waits_for_human_review() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="dag_agent",
                        arguments={
                            "request": "Create a safe DAG.",
                            "reason": "Needs reviewable orchestration.",
                        },
                    )
                ]
            ),
            ChatResponse(content=_dag_agent_json()),
        ]
    )
    node_loop = CompletingLoop()
    runtime = _runtime(provider, node_loop=node_loop)

    result = run(runtime.handle_message("Create a safe DAG", mode="auto"))

    assert result.status == "awaiting_dag_review"
    assert result.dag is not None
    assert result.dag.status == "review_required"
    assert node_loop.calls == 0


def test_harness_runtime_resume_dag_returns_top_agent_summary() -> None:
    final_answer = "final answer: echo:ok"
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="dag_agent",
                        arguments={
                            "request": "Create a safe DAG.",
                            "reason": "Needs execution.",
                        },
                    )
                ]
            ),
            ChatResponse(content=_dag_agent_json()),
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


def test_harness_runtime_dag_agent_gets_previous_dag_context_on_followup() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="dag_agent",
                        arguments={
                            "request": "Inspect the current files.",
                            "reason": "Needs reviewable orchestration.",
                        },
                    )
                ]
            ),
            ChatResponse(content=_dag_agent_json()),
            ChatResponse(content="The DAG result was echo:ok."),
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_2",
                        name="dag_agent",
                        arguments={
                            "request": "Use the previous result for the follow-up.",
                            "reason": "Follow-up needs orchestration.",
                        },
                    )
                ]
            ),
            ChatResponse(content=_dag_agent_json()),
        ]
    )
    runtime = _runtime(provider)

    first = run(runtime.handle_message("What files are here?", mode="auto"))
    resumed = run(runtime.resume_dag(first.task_id, first.dag))
    second = run(runtime.handle_message("What about the previous result?", mode="auto"))

    assert resumed.status == "completed"
    assert second.status == "awaiting_dag_review"
    dag_agent_requests = [
        request
        for request in provider.requests
        if any("Use this prior conversation and DAG execution context" in message.get("content", "") for message in request["messages"])
    ]
    assert dag_agent_requests
    followup_prompt = "\n".join(message.get("content", "") for message in dag_agent_requests[-1]["messages"])
    assert "What files are here?" in followup_prompt
    assert "echo:ok" in followup_prompt
    assert first.task_id in followup_prompt


def test_harness_runtime_dag_mode_forces_reviewable_dag_without_top_tool_call() -> None:
    provider = MockProvider([ChatResponse(content=_dag_agent_json())])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("Create a safe DAG", mode="dag"))

    assert result.status == "awaiting_dag_review"
    assert result.dag is not None
    assert result.dag.status == "review_required"
    assert result.task_id in runtime.tasks
    assert len(provider.requests) == 1


def test_harness_runtime_dag_mode_answers_after_dag_observation() -> None:
    provider = MockProvider(
        [
            ChatResponse(content=_dag_agent_json()),
            ChatResponse(content="final dag-mode answer"),
        ]
    )
    runtime = _runtime(provider)

    first = run(runtime.handle_message("Create a safe DAG", mode="dag"))
    resumed = run(runtime.resume_dag(first.task_id, first.dag))

    assert resumed.status == "completed"
    assert resumed.message_markdown == "final dag-mode answer"
    assert resumed.run_result is not None
    assert resumed.run_result.node_results["node_1"].final_response == "echo:ok"
    tool_names = [
        tool["function"]["name"]
        for tool in provider.requests[-1]["tools"]
    ]
    assert tool_names == ["dag_agent"]


def test_harness_runtime_dag_mode_can_create_continuation_dag_after_observation() -> None:
    provider = MockProvider(
        [
            ChatResponse(content=_dag_agent_json()),
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_continue",
                        name="dag_agent",
                        arguments={
                            "request": "Continue from the observed echo result.",
                            "reason": "The first DAG segment was not enough.",
                        },
                    )
                ]
            ),
            ChatResponse(content=_dag_agent_json(node_id="node_2", text="next")),
        ]
    )
    runtime = _runtime(provider)

    first = run(runtime.handle_message("Create a safe DAG", mode="dag"))
    resumed = run(runtime.resume_dag(first.task_id, first.dag))

    assert resumed.status == "awaiting_dag_review"
    assert resumed.task_id == first.task_id
    assert resumed.dag is not None
    assert resumed.dag.task_id == first.task_id
    assert resumed.dag.nodes[0].id == "node_2"
    assert runtime.tasks[first.task_id].continuation_count == 1
    assert runtime.tasks[first.task_id].node_results["node_1"].final_response == "echo:ok"
    assert len(runtime.tasks) == 1
    continuation_request = provider.requests[-1]
    prompt = "\n".join(message.get("content", "") for message in continuation_request["messages"])
    assert "echo:ok" in prompt
    tool_names = [
        tool["function"]["name"]
        for tool in provider.requests[-2]["tools"]
    ]
    assert tool_names == ["dag_agent"]


def test_harness_runtime_retries_dag_creation_with_validation_feedback() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                content=json.dumps(
                    {
                        "dag_id": "bad_full_dag",
                        "task_id": "ignored",
                        "nodes": [
                            _full_node("a"),
                            _full_node("b"),
                        ],
                        "edges": [],
                    }
                )
            ),
            ChatResponse(
                content=json.dumps(
                    {
                        "task": "fixed",
                        "nodes": [
                            {
                                "id": "start",
                                "tool": "dag_start",
                                "args": {},
                                "depends_on": [],
                            },
                            {
                                "id": "a",
                                "tool": "echo",
                                "args": {"text": "a"},
                                "depends_on": ["start"],
                            },
                            {
                                "id": "b",
                                "tool": "echo",
                                "args": {"text": "b"},
                                "depends_on": ["start"],
                            },
                        ],
                    }
                )
            ),
        ]
    )
    runtime = _runtime(provider)

    result = run(runtime.handle_message("Create a fixed DAG", mode="dag"))

    assert result.status == "awaiting_dag_review"
    assert result.dag is not None
    assert [node.id for node in result.dag.nodes] == ["start", "a", "b"]
    assert len(provider.requests) == 2
    assert "failed validation" in provider.requests[1]["messages"][-1]["content"]
    assert "Isolated node IDs" in provider.requests[1]["messages"][-1]["content"]


def _runtime(
    provider: MockProvider,
    *,
    node_loop: CompletingLoop | None = None,
    auto_execute_approved_dags: bool = False,
) -> HarnessRuntime:
    tool_executor = make_tool_executor()
    agent_loop = AgentLoop(provider=provider, tool_executor=tool_executor)
    dag_agent = LLMDAGAgent(provider, profile=_dag_agent_profile())
    return HarnessRuntime(
        agent_loop=agent_loop,
        dag_agent=dag_agent,
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
        name="dag_start",
        handler=lambda: "started",
        action="read",
        parameters={
            "type": "object",
            "properties": {},
        },
    )
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


def _dag_agent_profile() -> AgentProfile:
    return AgentProfile(
        name="dag_agent",
        role="dag_agent",
        layers=["soul"],
        layer_contents={"soul": "You are a DAG creator."},
    )


def _dag_agent_json(
    *,
    tools: list[str] | None = None,
    node_id: str = "node_1",
    text: str = "ok",
) -> str:
    tool = (tools or ["echo"])[0]
    args = {"path": "notes.md", "content": "hi"} if tool == "write_file" else {"text": text}
    boundary = {
        "mode": "write_limited" if tool == "write_file" else "read_only",
        "allowed_paths": ["notes.md"] if tool == "write_file" else [],
        "allowed_commands": [],
    }
    return json.dumps(
        {
            "dag_id": "dag_runtime",
            "task_id": "ignored",
            "version": 1,
            "status": "draft",
            "nodes": [
                {
                    "id": node_id,
                    "tool": tool,
                    "args": args,
                    "boundary": boundary,
                    "risk": "low",
                }
            ],
            "edges": [],
        }
    )


def _full_node(node_id: str) -> dict:
    return {
        "id": node_id,
        "tool": "echo",
        "args": {"text": node_id},
        "boundary": {
            "mode": "read_only",
            "allowed_paths": [],
            "allowed_commands": [],
        },
        "risk": "low",
    }
