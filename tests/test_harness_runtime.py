import asyncio
from dagent import capability
from dagent.harness_runtime import (
    ToolAgent,
    ToolAgentLoop,
    DAGAgent,
    DAGAgentLoop,
    DAGExecutor,
    HarnessRuntime,
    ValidatorAgent,
    CapabilityExecutor,
)
from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import (
    Artifact,
    Boundary,
    CapabilityInvocation,
    DAGNode,
    DAGSpec,
    RunTrace,
    RunTraceNode,
    ValidationIssue,
    ValidationResult,
)
from dagent.capabilities.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def dag_node_trace(trace: RunTrace, node_id: str) -> RunTraceNode:
    for child in trace.root.children:
        if child.kind == "dag_node" and child.ref.get("node_id") == node_id:
            return child
    raise AssertionError(f"Missing dag_node trace for {node_id}")


def capability_trace(trace: RunTrace, capability_id: str) -> RunTraceNode:
    stack = [trace.root]
    while stack:
        node = stack.pop(0)
        if node.kind == "capability_call" and node.ref.get("capability_id") == capability_id:
            return node
        stack[0:0] = node.children
    raise AssertionError(f"Missing capability_call trace for {capability_id}")


def test_harness_runtime_injects_registry_tools_into_dag_agent() -> None:
    provider = MockProvider([ChatResponse(content="unused")])
    runtime = _runtime(provider)

    tool_names = {tool.name for tool in runtime.dag_agent.tools}
    assert tool_names == {"echo", "fail_tool", "write_file"}


def test_harness_runtime_session_owns_dag_task_store() -> None:
    provider = MockProvider([ChatResponse(content="unused")])
    runtime = _runtime(provider)

    assert not hasattr(runtime.dag_agent, "tasks")
    assert not hasattr(runtime.dag_agent.loop, "tasks")
    assert runtime.tasks is runtime.session.tasks


def test_harness_runtime_reuses_executor_catalog_without_assembling_capabilities() -> None:
    provider = MockProvider([ChatResponse(content="unused")])
    capability_executor = make_capability_executor()
    tool_adapter = _tool_adapter(capability_executor.catalog)
    tool_agent = ToolAgent(
        loop=ToolAgentLoop(
            provider=provider,
            capability_executor=capability_executor,
            tool_adapter=tool_adapter,
        ),
        profile=_conversation_profile(),
    )
    dag_executor = DAGExecutor(capability_executor=capability_executor)
    runtime = HarnessRuntime(
        provider=provider,
        tool_agent=tool_agent,
        dag_agent=DAGAgent(
            loop=DAGAgentLoop(
                provider=provider,
                dag_executor=dag_executor,
                tool_adapter=tool_adapter,
            ),
            profile=_dag_agent_profile(),
        ),
        capability_executor=capability_executor,
    )

    assert runtime.capability_catalog is capability_executor.catalog
    assert "memory.write" not in runtime.capability_catalog.ids()


def test_harness_runtime_registers_and_replaces_public_capabilities() -> None:
    runtime = _runtime(MockProvider([ChatResponse(content="unused")]))
    agent_config = {"tool_adapter": runtime.tool_agent.loop.tool_adapter}
    runtime._agent_capability_configs.append(agent_config)

    @capability(id="custom_tool.echo2", name="echo2")
    def echo2(text: str) -> str:
        return f"first:{text}"

    registered = runtime.register_capability(echo2)
    invocation = CapabilityInvocation(
        capability_id="custom_tool.echo2",
        kind="custom_tool",
        arguments={"text": "ok"},
    )
    first = run(runtime.capability_executor.execute(invocation))

    @capability(id="custom_tool.echo2", name="echo2")
    def echo2_replacement(text: str) -> str:
        return f"second:{text}"

    replaced = runtime.replace_capability(echo2_replacement)
    second = run(runtime.capability_executor.execute(invocation))

    assert registered.id == "custom_tool.echo2"
    assert replaced.id == "custom_tool.echo2"
    assert first.content == "first:ok"
    assert second.content == "second:ok"
    assert runtime.tool_agent.loop.tool_adapter.function_name_for_capability(
        "custom_tool.echo2",
        enabled_toolsets=("builtin",),
    ) == "echo2"
    assert runtime.dag_agent.loop.tool_adapter.function_name_for_capability(
        "custom_tool.echo2",
        enabled_toolsets=("builtin",),
    ) == "echo2"
    assert agent_config["tool_adapter"] is runtime.tool_agent.loop.tool_adapter


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
    assert record.trace is not None
    assert record.trace.status == "completed"
    assert record.trace.root.output == "hello"


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
    assert result.dag.nodes[0].payload.invocation.risk == "medium"
    assert result.task_id in runtime.tasks
    assert runtime.tasks[result.task_id].runtime_mode == "dag"


def test_harness_runtime_dag_mode_direct_answer_does_not_expose_seed_dag() -> None:
    provider = MockProvider([ChatResponse(content="A direct answer.")])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("Answer directly if no DAG is needed.", mode="dag"))

    assert result.status == "completed"
    assert result.final_answer == "A direct answer."
    assert result.dag is None
    assert result.events == []


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
    assert resumed.trace is not None
    assert resumed.trace.status == "completed"
    assert resumed.final_answer == "Here is the final answer."
    runtime_task = runtime.session.tasks[result.task_id]
    assert runtime_task.mode == "dag"
    assert runtime_task.dag is not None
    assert capability_trace(runtime_task.trace, "tool.echo").status == "completed"


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


def test_harness_runtime_preserves_dag_review_when_approved_without_submitted_dag() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),
        ChatResponse(content="Here is the final answer."),
    ])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("What files are here?", mode="dag", review_level="careful"))
    missing_dag = run(runtime.resume_review(result.pending_review.review_id, approved=True))
    resumed = run(runtime.resume_review(result.pending_review.review_id, dag=result.dag))

    assert result.status == "awaiting_review"
    assert result.pending_review is not None
    assert missing_dag is None
    assert resumed is not None
    assert resumed.status == "completed"
    assert resumed.final_answer == "Here is the final answer."


def test_harness_runtime_retries_denied_dag_review_continuation_with_validation_feedback() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),
        ChatResponse(content="inspect = missing_tool()"),
        ChatResponse(content="I will stop instead of applying that DAG."),
    ])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("What files are here?", mode="dag", review_level="careful"))
    resumed = run(runtime.resume_review(result.pending_review.review_id, approved=False))

    assert result.status == "awaiting_review"
    assert resumed.status == "completed"
    assert resumed.final_answer == "I will stop instead of applying that DAG."
    assert len(provider.requests) == 3
    retry_content = provider.requests[2]["messages"][-1]["content"]
    assert "DAG observation: validation_error" in retry_content
    assert "Unknown capability function 'missing_tool'" in retry_content


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
    assert resumed.trace is not None
    assert dag_node_trace(resumed.trace, "node_1").output == "echo:ok"


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
    assert runtime.tasks[first.task_id].trace is not None
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
            content='a = echo(text="a")\nb = echo(text="b") after a'
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
    assert result.dag.nodes[0].payload.invocation.capability_id == "tool.echo"
    assert len(provider.requests) == 2
    feedback = provider.requests[1]["messages"][-1]["content"]
    assert "Unknown capability function 'get_current_dir'" in feedback
    assert "Available functions:" in feedback
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
                "inspect = echo(text=\"ok\")\n"
            )
        ),
        ChatResponse(content="The DAG completed after inspection."),
    ])
    runtime = _runtime(provider)
    runtime.dag_agent.loop.max_cycles = 3

    result = run(runtime.handle_message("Where am I?", mode="dag", review_level="fast"))

    assert result.status == "completed"
    assert result.final_answer == "The DAG completed after inspection."
    assert result.trace is not None
    assert result.trace.status == "completed"
    assert dag_node_trace(result.trace, "inspect").output == "echo:ok"


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


def test_tool_agent_loop_returns_tool_error_for_unknown_tool_call() -> None:
    provider = MockProvider([
        ChatResponse(tool_calls=[
            ToolCall(id="call_unknown", name="missing_tool", arguments={"text": "hi"}),
        ]),
        ChatResponse(content="recovered"),
    ])
    runtime = _runtime(provider)

    result = run(runtime.handle_message("Use the right tool.", mode="tool"))

    assert result.status == "completed"
    assert result.final_answer == "recovered"
    retry_messages = provider.requests[1]["messages"]
    assert retry_messages[-1]["role"] == "tool"
    assert retry_messages[-1]["tool_call_id"] == "call_unknown"
    assert retry_messages[-1]["name"] == "missing_tool"
    assert "[TOOL_ERROR]" in retry_messages[-1]["content"]
    assert "missing_tool" in retry_messages[-1]["content"]
    assert "Available tools:" in retry_messages[-1]["content"]


def test_resume_review_retries_when_validator_rejects_after_tool_approval() -> None:
    capability_executor = make_capability_executor()
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
    tool_adapter = _tool_adapter(capability_executor.catalog)
    tool_agent_loop = ToolAgentLoop(
        provider=provider,
        capability_executor=capability_executor,
        tool_adapter=tool_adapter,
    )
    dag_executor = DAGExecutor(capability_executor=capability_executor)
    runtime = HarnessRuntime(
        provider=provider,
        tool_agent=ToolAgent(
            loop=tool_agent_loop,
            profile=_conversation_profile(),
        ),
        dag_agent=DAGAgent(
            loop=DAGAgentLoop(
                provider=provider,
                dag_executor=dag_executor,
                tool_adapter=tool_adapter,
            ),
            profile=_dag_agent_profile(),
        ),
        validator=_RejectThenApproveValidator(),
        enable_validation=True,
    )

    first = run(runtime.handle_message("Write a note", mode="tool", review_level="careful"))
    resumed = run(runtime.resume_review(first.pending_review.review_id, approved=True))

    assert first.status == "awaiting_review"
    assert first.task_id is not None
    assert first.pending_review is not None
    assert first.pending_review.kind == "capability_review"
    assert first.pending_review.capability_call == {
        "invocation_id": "call_1",
        "capability_id": "tool.write_file",
        "arguments": {"path": "notes.md", "content": "hi"},
    }
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
    assert tool_tasks[0].trace is not None
    assert tool_tasks[0].trace.status == "completed"
    # write_file runs under the conversation's read_only boundary, so the approved
    # call settles as a boundary failure instead of a stale awaiting_review node.
    assert capability_trace(tool_tasks[0].trace, "tool.write_file").status == "failed"
    retry_request = provider.requests[2]["messages"]
    assert "Please address these issues." in retry_request[-1]["content"]


def test_resume_review_dag_validation_retry_preserves_task_identity() -> None:
    capability_executor = make_capability_executor()
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),        # initial DAG review
        ChatResponse(content="bad answer"),            # approved DAG execution
        ChatResponse(content=_dag_agent_dsl(text="retry")),  # validation retry DAG
        ChatResponse(content="good answer"),           # retry DAG execution
    ])
    tool_adapter = _tool_adapter(capability_executor.catalog)
    runtime = HarnessRuntime(
        provider=provider,
        tool_agent=ToolAgent(
            loop=ToolAgentLoop(
                provider=provider,
                capability_executor=capability_executor,
                tool_adapter=tool_adapter,
            ),
            profile=_conversation_profile(),
        ),
        dag_agent=DAGAgent(
            loop=DAGAgentLoop(
                provider=provider,
                dag_executor=DAGExecutor(capability_executor=capability_executor),
                tool_adapter=tool_adapter,
            ),
            profile=_dag_agent_profile(),
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
    assert record.trace is not None
    assert record.trace.run_id == first.task_id


def test_harness_runtime_skips_invalid_json_validator_agent_response() -> None:
    provider = MockProvider([
        ChatResponse(content=_dag_agent_dsl()),       # DAG agent
        ChatResponse(content="NO_CHANGE"),            # execute observation
        ChatResponse(content="looks fine to me"),     # validator agent, invalid JSON
    ])
    capability_executor = make_capability_executor()
    tool_adapter = _tool_adapter(capability_executor.catalog)
    runtime = HarnessRuntime(
        provider=provider,
        tool_agent=ToolAgent(
            loop=ToolAgentLoop(
                provider=provider,
                capability_executor=capability_executor,
                tool_adapter=tool_adapter,
            ),
            profile=_conversation_profile(),
        ),
        dag_agent=DAGAgent(
            loop=DAGAgentLoop(
                provider=provider,
                dag_executor=DAGExecutor(capability_executor=capability_executor),
                tool_adapter=tool_adapter,
            ),
            profile=_dag_agent_profile(),
        ),
        validator=ValidatorAgent(provider=provider, profile=_validator_profile()),
        enable_validation=True,
    )

    result = run(runtime.handle_message("Create a safe DAG", mode="dag", review_level="fast"))

    assert result.status == "completed"
    assert result.final_answer.startswith("DAG execution completed.")


def test_harness_runtime_run_dag_spec_records_loop_outcome_metadata(tmp_path) -> None:
    runtime = _runtime(MockProvider([ChatResponse(content="unused")]))
    spec = DAGSpec(
        id="write_note",
        name="Write note",
        artifacts={},
        nodes=[
            DAGNode(
                id="write",
                payload=dict(
                    type="capability",
                    invocation=CapabilityInvocation(
                        capability_id="tool.write_file",
                        kind="tool",
                        arguments={"path": "notes/output.txt", "content": "hi"},
                        boundary=Boundary(mode="write_limited", allowed_paths=["notes/output.txt"]),
                    ),
                ),
            )
        ],
    )

    dag_run = run(runtime.run_dag_spec(spec, workspace_root=tmp_path / "runs"))

    record = runtime.tasks[dag_run.run_id]
    assert record.mode == "dag"
    assert record.runtime_mode == "dag_spec"
    assert record.spec_id == "write_note"
    assert record.workspace_path == dag_run.workspace_path
    assert record.trace is not None
    assert dag_node_trace(record.trace, "write").status == "completed"


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
    capability_executor = make_capability_executor()
    tool_adapter = _tool_adapter(capability_executor.catalog)
    tool_agent_loop = ToolAgentLoop(
        provider=provider,
        capability_executor=capability_executor,
        tool_adapter=tool_adapter,
    )
    tool_agent = ToolAgent(
        loop=tool_agent_loop,
        profile=_conversation_profile(),
    )
    dag_executor = DAGExecutor(capability_executor=capability_executor)
    dag_agent_loop = DAGAgentLoop(
        provider=provider,
        dag_executor=dag_executor,
        tool_adapter=tool_adapter,
    )
    return HarnessRuntime(
        provider=provider,
        tool_agent=tool_agent,
        dag_agent=DAGAgent(
            loop=dag_agent_loop,
            profile=_dag_agent_profile(),
        ),
        capability_catalog=capability_executor.catalog,
        capability_executor=capability_executor,
    )


def make_capability_executor() -> CapabilityExecutor:
    tool_registry = ToolRegistry()
    tool_registry.register(
        name="echo",
        handler=lambda text: f"echo:{text}",
        action="read",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    tool_registry.register(
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
    tool_registry.register(
        name="fail_tool",
        handler=lambda text: (_ for _ in ()).throw(RuntimeError(f"failed:{text}")),
        action="read",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    capability_catalog = CapabilityCatalog()
    capability_executor = CapabilityExecutor(capability_catalog)
    ToolCapabilityProvider(tool_registry).register_into(capability_catalog)
    return capability_executor


def _tool_adapter(catalog: CapabilityCatalog) -> CapabilityToolAdapter:
    return CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset("builtin", tuple(sorted(catalog.ids())))],
    )


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
