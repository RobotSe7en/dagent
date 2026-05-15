import asyncio
from dagent.harness_runtime import (
    ToolAgent,
    ToolAgentLoop,
    DAGAgent,
    DAGAgentLoop,
    DAGExecutor,
    HarnessRuntime,
    ValidatorAgent,
)
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import ValidationIssue, ValidationResult
from dagent.tools.executor import ToolExecutor
from dagent.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def test_harness_runtime_injects_registry_tools_into_dag_agent() -> None:
    provider = MockProvider([ChatResponse(content="unused")])
    runtime = _runtime(provider)

    tool_names = {tool.name for tool in runtime.dag_agent.tools}
    assert tool_names == {"dag_start", "echo", "fail_tool", "write_file"}


def test_harness_runtime_session_owns_dag_task_store() -> None:
    provider = MockProvider([ChatResponse(content="unused")])
    runtime = _runtime(provider)

    assert not hasattr(runtime.dag_agent, "tasks")
    assert not hasattr(runtime.dag_agent.loop, "tasks")
    assert runtime.tasks is runtime.session.tasks


def test_harness_runtime_tool_message_does_not_create_dag() -> None:
    provider = MockProvider([
        ChatResponse(content="tool"),         # _route()
        ChatResponse(content="hello"),        # ToolAgentLoop
    ])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("hello", mode="auto"))

    assert result.status == "completed"
    assert result.final_answer == "hello"
    assert result.dag is None
    assert result.task_id is not None
    assert len(runtime.tasks) == 1
    record = runtime.tasks[result.task_id]
    assert record.mode == "tool"
    assert record.status == "completed"
    assert record.final_response == "hello"


def test_harness_runtime_tool_followup_uses_tool_agent_thread() -> None:
    provider = MockProvider([
        ChatResponse(content="The project color is blue."),  # ToolAgentLoop
        ChatResponse(content="It is blue."),                 # ToolAgentLoop
    ])
    runtime = _runtime(provider)

    first = run(runtime.handle_message("Remember that the project color is blue.", mode="tool"))
    second = run(runtime.handle_message("What color did I mention?", mode="tool"))

    assert first.status == "completed"
    assert second.status == "completed"
    # requests[0] = ToolAgentLoop, requests[1] = ToolAgentLoop (with history)
    second_agent_messages = provider.requests[1]["messages"]
    assert [message["role"] for message in second_agent_messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert second_agent_messages[1]["content"] == "Remember that the project color is blue."
    assert second_agent_messages[2]["content"] == "The project color is blue."
    assert second_agent_messages[3]["content"] == "What color did I mention?"


def test_harness_runtime_dag_planning_does_not_import_tool_thread() -> None:
    provider = MockProvider([
        ChatResponse(content="The project color is blue."),  # ToolAgentLoop
        ChatResponse(content=_dag_agent_dsl()),              # DAG agent
    ])
    runtime = _runtime(provider)

    first = run(runtime.handle_message("Remember that the project color is blue.", mode="tool"))
    second = run(runtime.handle_message("Use that color in a DAG task.", mode="dag", review_level="careful"))

    assert first.status == "completed"
    assert second.status == "awaiting_review"
    assert second.pending_review is not None
    assert second.pending_review.kind == "initial_dag"
    assert second.final_answer == ""
    planning_messages = provider.requests[1]["messages"]
    assert [message["role"] for message in planning_messages] == [
        "system",
        "user",
    ]
    assert planning_messages[0]["role"] == "system"
    assert "Use that color in a DAG task." in planning_messages[1]["content"]
    assert "Remember that the project color is blue." not in planning_messages[1]["content"]


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

    assert result.status == "awaiting_review"
    assert result.pending_review is not None
    assert result.pending_review.kind == "initial_dag"
    assert result.dag is not None


def test_harness_runtime_dag_agent_creates_reviewable_dag() -> None:
    provider = MockProvider([
        ChatResponse(content="dag"),                              # _route()
        ChatResponse(content=_dag_agent_dsl(tools=["write_file"])),  # DAG agent
    ])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("Do a complex risky task", mode="auto", review_level="careful"))

    assert result.status == "awaiting_review"
    assert result.pending_review is not None
    assert result.pending_review.kind == "initial_dag"
    assert result.dag is not None
    assert result.dag.status == "review_required"
    assert result.dag.nodes[0].invocation.risk == "medium"
    assert result.task_id in runtime.tasks
    assert runtime.tasks[result.task_id].runtime_mode == "dag"


def test_harness_runtime_dag_agent_waits_for_human_review() -> None:
    provider = MockProvider([
        ChatResponse(content="dag"),           # _route()
        ChatResponse(content=_dag_agent_dsl()),  # DAG agent
    ])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("Create a safe DAG", mode="auto", review_level="careful"))

    assert result.status == "awaiting_review"
    assert result.pending_review is not None
    assert result.pending_review.kind == "initial_dag"
    assert result.dag is not None
    assert result.dag.status == "review_required"


def test_harness_runtime_resume_review_for_dag_returns_final_answer() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),     # DAG agent (dag mode, no routing)
        ChatResponse(content="Here is the final answer."),  # execute loop observation
    ])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("What files are here?", mode="dag", review_level="careful"))
    resumed = run(runtime.resume_review(result.pending_review.review_id, dag=result.dag))

    assert result.status == "awaiting_review"
    assert result.pending_review is not None
    assert result.pending_review.kind == "initial_dag"
    assert resumed.status == "completed"
    assert resumed.dag_run is not None
    assert resumed.dag_run.completed is True
    assert resumed.dag_run.execution_records
    assert resumed.final_answer == "Here is the final answer."
    runtime_task = runtime.session.tasks[result.task_id]
    assert runtime_task.mode == "dag"
    assert runtime_task.dag_state is not None
    assert runtime_task.invocations
    assert runtime_task.execution_records
    assert runtime_task.execution_records[0].source == "dag_node"
    assert runtime_task.execution_records[0].invocation.runnable_id == "tool.echo"


def test_harness_runtime_rejects_dag_review_without_submitted_dag() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),
        ChatResponse(content="I will stop instead of applying that DAG."),
    ])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("What files are here?", mode="dag", review_level="careful"))
    resumed = run(runtime.resume_review(result.pending_review.review_id, approved=False))

    assert result.status == "awaiting_review"
    assert result.pending_review is not None
    assert resumed.status == "completed"
    assert resumed.final_answer == "I will stop instead of applying that DAG."
    assert any(event["kind"] == "review_denied" for event in resumed.events)
    resume_messages = provider.requests[1]["messages"]
    assert [message["role"] for message in resume_messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert "DAG observation: review_denied" in resume_messages[-1]["content"]


def test_harness_runtime_dag_agent_keeps_its_own_thread_on_followup() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),     # DAG agent (dag mode)
        ChatResponse(content="The result was echo:ok."),  # execute loop observation
        ChatResponse(content="dag"),                 # _route() for second message
        ChatResponse(content=_dag_agent_dsl()),      # DAG agent for follow-up
    ])
    runtime = _runtime(provider)

    first = run(runtime.handle_message("What files are here?", mode="dag", review_level="careful"))
    resumed = run(runtime.resume_review(first.pending_review.review_id, dag=first.dag))
    second = run(runtime.handle_message("What about the previous result?", mode="auto", review_level="careful"))

    assert resumed.status == "completed"
    assert second.status == "awaiting_review"
    assert second.pending_review is not None
    assert second.pending_review.kind == "initial_dag"
    assert second.final_answer == ""
    followup_messages = provider.requests[3]["messages"]
    assert [message["role"] for message in followup_messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    followup_prompt = "\n".join(message.get("content", "") for message in followup_messages)
    assert "What files are here?" in followup_prompt
    assert "The result was echo:ok." in followup_prompt
    assert "What about the previous result?" in followup_prompt


def test_harness_runtime_dag_mode_forces_reviewable_dag_without_top_tool_call() -> None:
    provider = MockProvider([ChatResponse(content=_dag_agent_dsl())])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("Create a safe DAG", mode="dag", review_level="careful"))

    assert result.status == "awaiting_review"
    assert result.pending_review is not None
    assert result.pending_review.kind == "initial_dag"
    assert result.dag is not None
    assert result.dag.status == "review_required"
    assert result.task_id in runtime.tasks
    assert len(provider.requests) == 1


def test_harness_runtime_dag_mode_answers_after_dag_observation() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),
        ChatResponse(content="final dag-mode answer"),
    ])
    runtime = _runtime(provider)

    first = run(runtime.handle_message("Create a safe DAG", mode="dag", review_level="careful"))
    resumed = run(runtime.resume_review(first.pending_review.review_id, dag=first.dag))

    assert resumed.status == "completed"
    assert resumed.final_answer == "final dag-mode answer"
    assert resumed.dag_run is not None
    assert resumed.dag_run.node_results["node_1"].final_response == "echo:ok"


def test_harness_runtime_review_id_cannot_be_reused_after_resume() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),
        ChatResponse(content="final dag-mode answer"),
    ])
    runtime = _runtime(provider)

    first = run(runtime.handle_message("Create a safe DAG", mode="dag", review_level="careful"))
    resumed = run(runtime.resume_review(first.pending_review.review_id, dag=first.dag))
    repeated = run(runtime.resume_review(first.pending_review.review_id, dag=resumed.dag))

    assert repeated is None
    assert len(runtime.tasks[first.task_id].runs) == 1
    assert len(provider.requests) == 2  # dag_agent + execute observation


def test_harness_runtime_dag_mode_fails_without_review() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl(tools=["fail_tool"], text="boom")),
        ChatResponse(content="NO_CHANGE"),
        ChatResponse(content="The DAG failed after exhausting repair attempts."),
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
        ChatResponse(content="all done"),
    ])
    runtime = _runtime(provider)

    first = run(runtime.handle_message("Create a safe DAG", mode="dag", review_level="careful"))
    resumed = run(runtime.resume_review(first.pending_review.review_id, dag=first.dag))

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

    assert result.status == "awaiting_review"
    assert result.pending_review is not None
    assert result.pending_review.kind == "initial_dag"
    assert result.dag is not None
    assert [node.id for node in result.dag.nodes] == ["start", "a", "b"]
    assert len(provider.requests) == 2
    retry_content = provider.requests[1]["messages"][-1]["content"]
    assert "validation error" in retry_content.lower() or "invalid" in retry_content.lower()
    assert "User request:" not in retry_content


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

    assert result.status == "awaiting_review"
    assert result.pending_review is not None
    assert result.pending_review.kind == "initial_dag"
    assert result.dag is not None
    assert result.dag.nodes[0].invocation.runnable_id == "tool.echo"
    assert len(provider.requests) == 2
    feedback = provider.requests[1]["messages"][-1]["content"]
    assert "Unknown tool(s): get_current_dir" in feedback
    assert "Available tools:" in feedback
    assert "echo" in feedback
    assert "User request:" not in feedback


def test_harness_runtime_planning_retry_does_not_stop_after_start_only() -> None:
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
                "start = dag_start()\n"
                "inspect = echo(text=\"ok\") after start\n"
            )
        ),
        ChatResponse(content="The DAG completed after inspection."),
    ])
    runtime = _runtime(provider)
    runtime.dag_agent.loop.max_cycles = 3

    result = run(runtime.handle_message("Where am I?", mode="dag", review_level="fast"))

    assert result.status == "completed"
    assert result.final_answer == "The DAG completed after inspection."
    assert result.dag_run is not None
    assert result.dag_run.completed is True
    assert result.dag_run.node_results["inspect"].final_response == "echo:ok"


def test_harness_runtime_auto_route_defaults_to_tool_on_error() -> None:
    """When the routing LLM call fails, default to tool mode."""
    call_count = [0]
    original_responses = [
        ChatResponse(content="hello world"),      # ToolAgentLoop response
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
    assert result.final_answer == "hello world"


def test_think_tag_filter_keep_inside_only_forwards_think_blocks() -> None:
    from dagent.harness_runtime.runtime_events import _ThinkTagFilter

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
    from dagent.harness_runtime.runtime_events import _ThinkTagFilter

    collected: list[str] = []
    filt = _ThinkTagFilter(collected.append, keep="inside")

    filt("Hello world, this is a normal response.")

    assert "".join(collected) == ""


def test_think_tag_filter_keep_outside_strips_thinking() -> None:
    from dagent.harness_runtime.runtime_events import _ThinkTagFilter

    collected: list[str] = []
    filt = _ThinkTagFilter(collected.append, keep="outside")

    for token in ["<think>", "internal reasoning", "</think>", "The final answer."]:
        filt(token)

    result = "".join(collected)
    assert "internal reasoning" not in result
    assert "<think>" not in result
    assert "The final answer." in result


def test_think_tag_filter_keep_outside_passes_all_when_no_think() -> None:
    from dagent.harness_runtime.runtime_events import _ThinkTagFilter

    collected: list[str] = []
    filt = _ThinkTagFilter(collected.append, keep="outside")

    filt("Hello world, no thinking here.")

    assert "Hello world" in "".join(collected)


def test_harness_runtime_tool_mode_only_streams_thinking_tokens() -> None:
    provider = MockProvider([
        ChatResponse(content="<think>reasoning</think>The answer."),  # ToolAgentLoop
    ])
    runtime = _runtime(provider)

    streamed: list[str] = []
    result = run(runtime.handle_message(
        "hello",
        mode="tool",
        on_token=streamed.append,
    ))

    full = "".join(streamed)
    # Thinking should be streamed
    assert "<think>" in full
    assert "reasoning" in full
    assert "</think>" in full
    # The answer from ToolAgentLoop is returned in final_answer, not streamed.
    assert result.final_answer == "The answer."


def test_resume_review_retries_when_validator_rejects_after_tool_approval() -> None:
    tool_executor = make_tool_executor()
    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="write_file",
                    arguments={"path": "notes.md", "content": "hi"},
                )
            ]
        ),
        ChatResponse(content="bad answer"),
        ChatResponse(content="good answer"),
    ])
    tool_agent_loop = ToolAgentLoop(provider=provider, tool_executor=tool_executor)
    dag_executor = DAGExecutor(tool_executor=tool_executor)
    runtime = HarnessRuntime(
        provider=provider,
        tool_agent=ToolAgent(
            loop=tool_agent_loop,
            profile=_conversation_profile(),
            tools=tool_executor.registry.all_tools(),
        ),
        dag_agent=DAGAgent(
            loop=DAGAgentLoop(
                provider=provider,
                dag_executor=dag_executor,
            ),
            profile=_dag_agent_profile(),
            tools=tool_executor.registry.all_tools(),
        ),
        validator=_RejectThenApproveValidator(),
        enable_validation=True,
    )

    first = run(runtime.handle_message("Write a note", mode="tool", review_level="careful"))
    resumed = run(runtime.resume_review(first.pending_review.review_id, approved=True))

    assert first.status == "awaiting_review"
    assert first.task_id is not None
    assert first.pending_review is not None
    assert first.pending_review.kind == "tool_review"
    assert resumed.status == "completed"
    assert resumed.task_id == first.task_id
    assert resumed.final_answer == "good answer"
    tool_tasks = [
        task
        for task in runtime.session.tasks.values()
        if task.mode == "tool"
    ]
    assert len(tool_tasks) == 1
    assert tool_tasks[0].task_id == first.task_id
    assert tool_tasks[0].status == "completed"
    assert tool_tasks[0].tool_state is not None
    assert tool_tasks[0].invocations
    assert tool_tasks[0].execution_records
    assert tool_tasks[0].execution_records[0].source == "tool_loop"
    assert tool_tasks[0].execution_records[0].invocation.runnable_id == "tool.write_file"
    retry_request = provider.requests[2]["messages"]
    assert "Please address these issues." in retry_request[-1]["content"]


def test_resume_review_dag_validation_retry_preserves_task_identity() -> None:
    tool_executor = make_tool_executor()
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),        # initial DAG review
        ChatResponse(content="bad answer"),            # approved DAG execution
        ChatResponse(content=_dag_agent_dsl(text="retry")),  # validation retry DAG
        ChatResponse(content="good answer"),           # retry DAG execution
    ])
    runtime = HarnessRuntime(
        provider=provider,
        tool_agent=ToolAgent(
            loop=ToolAgentLoop(provider=provider, tool_executor=tool_executor),
            profile=_conversation_profile(),
            tools=[],
        ),
        dag_agent=DAGAgent(
            loop=DAGAgentLoop(
                provider=provider,
                dag_executor=DAGExecutor(tool_executor=tool_executor),
            ),
            profile=_dag_agent_profile(),
            tools=tool_executor.registry.all_tools(),
        ),
        validator=_RejectThenApproveValidator(),
        enable_validation=True,
    )

    first = run(runtime.handle_message("Create a reviewed DAG", mode="dag", review_level="careful"))
    resumed = run(runtime.resume_review(
        first.pending_review.review_id,
        dag=first.dag,
        review_level="fast",
    ))

    assert resumed.status == "completed"
    assert resumed.task_id == first.task_id
    assert resumed.dag is not None
    assert resumed.dag.task_id == first.task_id
    record = runtime.tasks[first.task_id]
    assert record.dag.task_id == first.task_id
    assert record.execution_records
    assert all(
        execution.task_id == first.task_id
        for execution in record.execution_records
    )


def test_harness_runtime_skips_invalid_json_validator_agent_response() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),       # DAG agent
        ChatResponse(content="NO_CHANGE"),            # execute observation
        ChatResponse(content="looks fine to me"),     # validator agent, invalid JSON
    ])
    tool_executor = make_tool_executor()
    runtime = HarnessRuntime(
        provider=provider,
        tool_agent=ToolAgent(
            loop=ToolAgentLoop(provider=provider, tool_executor=tool_executor),
            profile=_conversation_profile(),
        ),
        dag_agent=DAGAgent(
            loop=DAGAgentLoop(
                provider=provider,
                dag_executor=DAGExecutor(tool_executor=tool_executor),
            ),
            profile=_dag_agent_profile(),
            tools=tool_executor.registry.all_tools(),
        ),
        validator=ValidatorAgent(provider=provider, profile=_validator_profile()),
        enable_validation=True,
    )

    result = run(runtime.handle_message("Create a safe DAG", mode="dag", review_level="fast"))

    assert result.status == "completed"
    assert result.final_answer.startswith("DAG execution completed.")


class _RejectThenApproveValidator:
    def __init__(self) -> None:
        self.calls = 0

    async def validate(self, *, user_request: str, final_answer: str, execution_context: str = "") -> ValidationResult:
        self.calls += 1
        if self.calls == 1:
            return ValidationResult(passed=False,
                summary="Needs a better answer.",
                issues=[ValidationIssue(message="Try again.")],
            )
        return ValidationResult(passed=True, summary="ok")


def _runtime(
    provider: MockProvider,
) -> HarnessRuntime:
    tool_executor = make_tool_executor()
    tool_agent_loop = ToolAgentLoop(provider=provider, tool_executor=tool_executor)
    tool_agent = ToolAgent(
        loop=tool_agent_loop,
        profile=_conversation_profile(),
        tools=[],
    )
    dag_executor = DAGExecutor(tool_executor=tool_executor)
    dag_agent_loop = DAGAgentLoop(
        provider=provider,
        dag_executor=dag_executor,
    )
    return HarnessRuntime(
        provider=provider,
        tool_agent=tool_agent,
        dag_agent=DAGAgent(
            loop=dag_agent_loop,
            profile=_dag_agent_profile(),
            tools=tool_executor.registry.all_tools(),
        ),
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


def _validator_profile() -> AgentProfile:
    return AgentProfile(
        name="validator_agent",
        role="validator_agent",
        layers=["soul"],
        layer_contents={"soul": "You are a validator agent."},
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
