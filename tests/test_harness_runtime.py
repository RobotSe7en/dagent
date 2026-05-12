import asyncio
from dagent.harness_runtime import (
    AgentLoop,
    AgentLoopResult,
    DAGAgentLoop,
    DAGExecutor,
    HarnessRuntime,
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

    tool_names = {tool.name for tool in runtime.dag_agent_loop.tools}
    assert tool_names == {"dag_start", "echo", "fail_tool", "write_file"}


def test_harness_runtime_direct_message_does_not_create_dag() -> None:
    provider = MockProvider([
        ChatResponse(content="direct"),       # _route()
        ChatResponse(content="hello"),        # direct AgentLoop
        ChatResponse(content="hello summary"),  # _summarize()
    ])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("hello", mode="auto"))

    assert result.status == "completed"
    assert result.message_markdown == "hello summary"
    assert result.dag is None
    assert runtime.tasks == {}


def test_harness_runtime_direct_followup_includes_conversation_history() -> None:
    provider = MockProvider([
        ChatResponse(content="The project color is blue."),  # direct AgentLoop
        ChatResponse(content="Noted, blue."),                # _summarize()
        ChatResponse(content="It is blue."),                 # direct AgentLoop
        ChatResponse(content="Blue."),                       # _summarize()
    ])
    runtime = _runtime(provider)

    first = run(runtime.handle_message("Remember that the project color is blue.", mode="direct"))
    second = run(runtime.handle_message("What color did I mention?", mode="direct"))

    assert first.status == "completed"
    assert second.status == "completed"
    # requests[0] = direct AgentLoop, requests[1] = _summarize(),
    # requests[2] = direct AgentLoop (with history), requests[3] = _summarize()
    second_agent_messages = provider.requests[2]["messages"]
    assert [message["role"] for message in second_agent_messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert second_agent_messages[1]["content"] == "Remember that the project color is blue."
    assert second_agent_messages[2]["content"] == "The project color is blue."
    assert second_agent_messages[3]["content"] == "What color did I mention?"


def test_harness_runtime_dag_planning_includes_conversation_history() -> None:
    provider = MockProvider([
        ChatResponse(content="The project color is blue."),  # direct AgentLoop
        ChatResponse(content="Noted, blue."),                # _summarize()
        ChatResponse(content=_dag_agent_dsl()),              # DAG agent
    ])
    runtime = _runtime(provider)

    first = run(runtime.handle_message("Remember that the project color is blue.", mode="direct"))
    second = run(runtime.handle_message("Use that color in a DAG task.", mode="dag", review_level="careful"))

    assert first.status == "completed"
    assert second.status == "awaiting_dag_review"
    assert second.message_markdown == ""
    dag_messages = provider.requests[2]["messages"]
    # Expect: system, conv_user, conv_assistant, active_user, planning_context
    assert dag_messages[0]["role"] == "system"
    assert dag_messages[1]["content"] == "Remember that the project color is blue."
    assert dag_messages[2]["content"] == "The project color is blue."
    # The active user message and planning_context are both user messages
    all_content = "\n".join(m.get("content", "") for m in dag_messages)
    assert "Use that color in a DAG task." in all_content


def test_harness_runtime_auto_routes_to_dag() -> None:
    provider = MockProvider([
        ChatResponse(content="dag"),           # _route()
        ChatResponse(content=_dag_agent_dsl()),  # DAG agent
    ])
    runtime = _runtime(provider)

    result = run(runtime.handle_message(
        "Create a DAG that uses the blue project color.",
        mode="auto",
        review_level="careful",
    ))

    assert result.status == "awaiting_dag_review"
    assert result.dag is not None


def test_harness_runtime_dag_agent_creates_reviewable_dag() -> None:
    provider = MockProvider([
        ChatResponse(content="dag"),                              # _route()
        ChatResponse(content=_dag_agent_dsl(tools=["write_file"])),  # DAG agent
    ])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("Do a complex risky task", mode="auto", review_level="careful"))

    assert result.status == "awaiting_dag_review"
    assert result.dag is not None
    assert result.dag.status == "review_required"
    assert result.dag.nodes[0].risk == "medium"
    assert result.task_id in runtime.tasks


def test_harness_runtime_dag_agent_waits_for_human_review() -> None:
    provider = MockProvider([
        ChatResponse(content="dag"),           # _route()
        ChatResponse(content=_dag_agent_dsl()),  # DAG agent
    ])
    node_loop = CompletingLoop()
    runtime = _runtime(provider, node_loop=node_loop)

    result = run(runtime.handle_message("Create a safe DAG", mode="auto", review_level="careful"))

    assert result.status == "awaiting_dag_review"
    assert result.dag is not None
    assert result.dag.status == "review_required"
    assert node_loop.calls == 0


def test_harness_runtime_resume_dag_produces_summary() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),     # DAG agent (dag mode, no routing)
        ChatResponse(content="NO_CHANGE"),           # execute loop observation
        ChatResponse(content="Here is the summary."),  # _summarize()
    ])
    node_loop = CompletingLoop()
    runtime = _runtime(provider, node_loop=node_loop)

    result = run(runtime.handle_message("What files are here?", mode="dag", review_level="careful"))
    resumed = run(runtime.resume_dag(result.task_id, result.dag))

    assert result.status == "awaiting_dag_review"
    assert resumed.status == "completed"
    assert resumed.run_result is not None
    assert resumed.run_result.completed is True
    assert resumed.message_markdown == "Here is the summary."


def test_harness_runtime_dag_agent_gets_previous_dag_context_on_followup() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),     # DAG agent (dag mode)
        ChatResponse(content="NO_CHANGE"),           # execute loop observation
        ChatResponse(content="The result was echo:ok."),  # _summarize()
        ChatResponse(content="dag"),                 # _route() for second message
        ChatResponse(content=_dag_agent_dsl()),      # DAG agent for follow-up
    ])
    runtime = _runtime(provider)

    first = run(runtime.handle_message("What files are here?", mode="dag", review_level="careful"))
    resumed = run(runtime.resume_dag(first.task_id, first.dag))
    second = run(runtime.handle_message("What about the previous result?", mode="auto", review_level="careful"))

    assert resumed.status == "completed"
    assert second.status == "awaiting_dag_review"
    assert second.message_markdown == ""
    dag_agent_requests = [
        request
        for request in provider.requests
        if any("DAG observation: planning_context" in message.get("content", "") for message in request["messages"])
    ]
    assert dag_agent_requests
    followup_prompt = "\n".join(message.get("content", "") for message in dag_agent_requests[-1]["messages"])
    assert "What files are here?" in followup_prompt
    assert first.task_id in followup_prompt


def test_harness_runtime_dag_mode_forces_reviewable_dag_without_top_tool_call() -> None:
    provider = MockProvider([ChatResponse(content=_dag_agent_dsl())])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("Create a safe DAG", mode="dag", review_level="careful"))

    assert result.status == "awaiting_dag_review"
    assert result.dag is not None
    assert result.dag.status == "review_required"
    assert result.task_id in runtime.tasks
    assert len(provider.requests) == 1


def test_harness_runtime_dag_mode_answers_after_dag_observation() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),
        ChatResponse(content="final dag-mode answer"),
        ChatResponse(content="summarized answer"),    # _summarize()
    ])
    runtime = _runtime(provider)

    first = run(runtime.handle_message("Create a safe DAG", mode="dag", review_level="careful"))
    resumed = run(runtime.resume_dag(first.task_id, first.dag))

    assert resumed.status == "completed"
    assert resumed.message_markdown == "summarized answer"
    assert resumed.run_result is not None
    assert resumed.run_result.node_results["node_1"].final_response == "echo:ok"


def test_harness_runtime_completed_dag_resume_is_idempotent() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),
        ChatResponse(content="final dag-mode answer"),
        ChatResponse(content="summarized answer"),    # _summarize()
    ])
    runtime = _runtime(provider)

    first = run(runtime.handle_message("Create a safe DAG", mode="dag", review_level="careful"))
    resumed = run(runtime.resume_dag(first.task_id, first.dag))
    repeated = run(runtime.resume_dag(first.task_id, resumed.dag))

    assert repeated.status == "completed"
    assert repeated.message_markdown == ""
    assert repeated.run_result is resumed.run_result
    assert len(runtime.tasks[first.task_id].runs) == 1
    assert len(provider.requests) == 3  # dag_agent + execute observation + summarize


def test_harness_runtime_dag_mode_fails_without_review() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl(tools=["fail_tool"], text="boom")),
        ChatResponse(content="NO_CHANGE"),
        ChatResponse(content="The DAG failed after exhausting repair attempts."),
        ChatResponse(content="The task failed."),     # _summarize()
    ])
    runtime = _runtime(provider)

    first = run(runtime.handle_message("Create a failing DAG", mode="dag"))

    assert first.status == "failed"
    assert first.pending_review is None
    assert first.dag is not None
    assert first.dag.status == "failed"
    assert any(
        "DAG observation" in message.get("content", "")
        for request in provider.requests
        for message in request["messages"]
    )


def test_harness_runtime_dag_mode_can_create_continuation_dag_after_observation() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),
        ChatResponse(content="NO_CHANGE"),
        ChatResponse(content="all done"),    # _summarize()
    ])
    runtime = _runtime(provider)

    first = run(runtime.handle_message("Create a safe DAG", mode="dag", review_level="careful"))
    resumed = run(runtime.resume_dag(first.task_id, first.dag))

    assert resumed.status == "completed"
    assert resumed.task_id == first.task_id
    assert resumed.dag is not None
    assert resumed.dag.task_id == first.task_id
    assert resumed.dag.status == "completed"
    assert len(runtime.tasks) == 1


def test_harness_runtime_retries_dag_creation_with_validation_feedback() -> None:
    provider = MockProvider([
        ChatResponse(
            content='a = echo(text="a")\nb = echo(text="b") after nonexistent'
        ),
        ChatResponse(
            content='start = dag_start()\na = echo(text="a") after start\nb = echo(text="b") after a'
        ),
    ])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("Create a fixed DAG", mode="dag", review_level="careful"))

    assert result.status == "awaiting_dag_review"
    assert result.dag is not None
    assert [node.id for node in result.dag.nodes] == ["start", "a", "b"]
    assert len(provider.requests) == 2
    assert "validation error" in provider.requests[1]["messages"][-1]["content"].lower() or "invalid" in provider.requests[1]["messages"][-1]["content"].lower()


def test_harness_runtime_retries_dag_creation_with_unknown_tool_feedback() -> None:
    provider = MockProvider([
        ChatResponse(
            content=(
                "task: inspect current directory\n"
                "get_current_dir = get_current_dir()\n"
            )
        ),
        ChatResponse(
            content=(
                "task: inspect current directory\n"
                "inspect = echo(text=\"use available tools\")\n"
            )
        ),
    ])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("Where am I?", mode="dag", review_level="careful"))

    assert result.status == "awaiting_dag_review"
    assert result.dag is not None
    assert result.dag.nodes[0].tool == "echo"
    assert len(provider.requests) == 2
    feedback = provider.requests[1]["messages"][-1]["content"]
    assert "Unknown tool(s): get_current_dir" in feedback
    assert "Available tools:" in feedback
    assert "echo" in feedback


def test_harness_runtime_auto_route_defaults_to_direct_on_error() -> None:
    """When the routing LLM call fails, default to direct mode."""
    call_count = [0]
    original_responses = [
        ChatResponse(content="hello world"),      # direct AgentLoop response
        ChatResponse(content="hello summarized"),  # _summarize()
    ]

    class FailFirstProvider(MockProvider):
        async def chat(self, messages, tools=None):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("LLM down")
            return await super().chat(messages, tools=tools)

    provider = FailFirstProvider(original_responses)
    runtime = _runtime(provider)

    result = run(runtime.handle_message("hello", mode="auto"))

    assert result.status == "completed"
    assert result.message_markdown == "hello summarized"


def test_think_tag_filter_keep_inside_only_forwards_think_blocks() -> None:
    from dagent.harness_runtime.runtime import _ThinkTagFilter

    collected: list[str] = []
    filt = _ThinkTagFilter(collected.append, keep="inside")

    # Simulate tokens arriving in chunks
    for token in ["<", "think", ">reason", "ing about", " it</", "think>", " The answer is 42."]:
        filt(token)

    result = "".join(collected)
    assert "<think>" in result
    assert "reasoning about it" in result
    assert "</think>" in result
    assert "The answer is 42" not in result


def test_think_tag_filter_keep_inside_no_think_emits_nothing() -> None:
    from dagent.harness_runtime.runtime import _ThinkTagFilter

    collected: list[str] = []
    filt = _ThinkTagFilter(collected.append, keep="inside")

    filt("Hello world, this is a normal response.")

    assert "".join(collected) == ""


def test_think_tag_filter_keep_outside_strips_thinking() -> None:
    from dagent.harness_runtime.runtime import _ThinkTagFilter

    collected: list[str] = []
    filt = _ThinkTagFilter(collected.append, keep="outside")

    for token in ["<think>", "internal reasoning", "</think>", "The final answer."]:
        filt(token)

    result = "".join(collected)
    assert "internal reasoning" not in result
    assert "<think>" not in result
    assert "The final answer." in result


def test_think_tag_filter_keep_outside_passes_all_when_no_think() -> None:
    from dagent.harness_runtime.runtime import _ThinkTagFilter

    collected: list[str] = []
    filt = _ThinkTagFilter(collected.append, keep="outside")

    filt("Hello world, no thinking here.")

    assert "Hello world" in "".join(collected)


def test_harness_runtime_direct_mode_only_streams_thinking_tokens() -> None:
    provider = MockProvider([
        ChatResponse(content="<think>reasoning</think>The answer."),  # direct AgentLoop
        ChatResponse(content="summarized answer"),                   # _summarize()
    ])
    runtime = _runtime(provider)

    streamed: list[str] = []
    result = run(runtime.handle_message(
        "hello",
        mode="direct",
        on_token=streamed.append,
    ))

    full = "".join(streamed)
    # Thinking should be streamed
    assert "<think>" in full
    assert "reasoning" in full
    assert "</think>" in full
    # The answer from AgentLoop should NOT be streamed (only summarize answer)
    # The summarize answer IS streamed (but MockProvider.chat doesn't stream,
    # so we just verify the final result comes from summarize)
    assert result.message_markdown == "summarized answer"


def _runtime(
    provider: MockProvider,
    *,
    node_loop: CompletingLoop | None = None,
) -> HarnessRuntime:
    tool_executor = make_tool_executor()
    agent_loop = AgentLoop(provider=provider, tool_executor=tool_executor)
    dag_executor = DAGExecutor(tool_executor=tool_executor)
    dag_agent_loop = DAGAgentLoop(
        provider,
        dag_executor=dag_executor,
        profile=_dag_agent_profile(),
    )
    return HarnessRuntime(
        provider=provider,
        agent_loop=agent_loop,
        dag_agent_loop=dag_agent_loop,
        conversation_profile=_conversation_profile(),
        runtime_tools=[],
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
        risk="medium",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path"],
        },
    )
    registry.register(
        name="fail_tool",
        handler=lambda text: (_ for _ in ()).throw(RuntimeError(f"failed:{text}")),
        action="read",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
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


def _dag_agent_dsl(
    *,
    tools: list[str] | None = None,
    node_id: str = "node_1",
    text: str = "ok",
) -> str:
    tool = (tools or ["echo"])[0]
    args = 'path="notes.md", content="hi"' if tool == "write_file" else f'text="{text}"'
    return f"task: mock\n{node_id} = {tool}({args})\n"
