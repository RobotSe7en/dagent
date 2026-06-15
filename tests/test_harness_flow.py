import asyncio

from dagent.harness_runtime import (
    DAGAgent,
    DAGAgentLoop,
    DAGExecutor,
    HarnessRuntime,
    ToolAgent,
    ToolAgentLoop,
    CapabilityExecutor,
)
from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.providers import ChatResponse, MockProvider
from dagent.harness_runtime.dag_builder import parse_plan_spec_dsl
from dagent.profiles import AgentProfile
from dagent.schemas import (
    Boundary,
    CapabilityNodePayload,
    CapabilityDefinition,
    CapabilityInvocation,
    DAG,
    DAGEdge,
    DAGNode,
    RunTrace,
    RunTraceNode,
    RunState,
    StartNodePayload,
)
from dagent.capabilities.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def dag_node_trace(trace: RunTrace, node_id: str) -> RunTraceNode:
    for child in trace.root.children:
        if child.kind == "dag_node" and child.ref.get("node_id") == node_id:
            return child
    raise AssertionError(f"Missing dag_node trace for {node_id}")


def trace_with_completed_nodes(task_id: str, outputs: dict[str, str]) -> RunTrace:
    root = RunTraceNode.run(run_id=task_id, status="completed")
    root.children = [
        RunTraceNode.dag_node(
            parent_id=root.id,
            node_id=node_id,
            status="completed",
        )
        for node_id in outputs
    ]
    for child in root.children:
        child.output = outputs[child.ref["node_id"]]
    return RunTrace(run_id=task_id, root=root)


def dag_state(
    *,
    task_id: str,
    user_request: str,
    dag: DAG,
    review_level: str = "fast",
) -> RunState:
    return RunState(
        run_id=task_id,
        kind="dynamic_dag",
        status="completed",
        user_request=user_request,
        dag=dag,
        review_level=review_level,
        runtime_mode="dag",
    )


def runtime_for(
    *,
    dag_agent_loop: DAGAgentLoop,
    executor: DAGExecutor,
    max_cycles: int = 6,
) -> HarnessRuntime:
    dag_agent_loop.max_cycles = max_cycles
    return HarnessRuntime(
        provider=dag_agent_loop.provider,
        tool_agent=ToolAgent(
            loop=ToolAgentLoop(
                provider=dag_agent_loop.provider,
                capability_executor=executor.capability_executor,
                tool_adapter=_tool_adapter(executor.capability_executor.catalog),
            ),
            profile=AgentProfile(
                name="conversation",
                content="You are a conversation agent.",
            ),
        ),
        dag_agent=DAGAgent(
            loop=dag_agent_loop,
            profile=AgentProfile(
                name="dag_agent",
                content="You are a DAG creator.",
            ),
        ),
    )


def dag_loop_for(provider: MockProvider, executor: DAGExecutor | None = None) -> DAGAgentLoop:
    dag_executor = executor or DAGExecutor(capability_executor=make_capability_executor())
    return DAGAgentLoop(
        provider=provider,
        dag_executor=dag_executor,
        tool_adapter=_tool_adapter(dag_executor.capability_executor.catalog),
    )


def dag_agent_for(dag_loop: DAGAgentLoop) -> DAGAgent:
    return DAGAgent(
        loop=dag_loop,
        profile=AgentProfile(
            name="dag_agent",
            content="You are a DAG creator.",
        ),
    )


def dag_dsl_from_dag(
    dag: DAG,
    *,
    tool_adapter: CapabilityToolAdapter,
    enabled_toolsets: tuple[str, ...] = ("builtin",),
) -> str:
    lines = []
    for node in dag.nodes:
        if isinstance(node.payload, StartNodePayload):
            continue
        assert isinstance(node.payload, CapabilityNodePayload)
        deps = [e.source for e in dag.edges if e.target == node.id and e.source != "start"]
        deps_str = f" after {', '.join(deps)}" if deps else ""
        invocation = node.payload.invocation
        args = ", ".join(f'{k}={repr(v)}' for k, v in invocation.arguments.items())
        tool_name = _tool_name_from_capability(
            invocation.capability_id,
            tool_adapter=tool_adapter,
            enabled_toolsets=enabled_toolsets,
        )
        lines.append(f'{node.id} = {tool_name}({args}){deps_str}')
    return "\n".join(lines)


def make_capability_executor() -> CapabilityExecutor:
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
        name="run_command",
        handler=lambda command, cwd=".", timeout_seconds=30: f"ran:{command}:{cwd}",
        action="command",
        path_args=("cwd",),
        command_args=("command",),
        risk="high",
        default_args={"cwd": ".", "timeout_seconds": 30},
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
            },
            "required": ["command"],
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
    registry.register(
        name="fail_unless_fixed",
        handler=lambda text: (
            "fixed-ok"
            if text == "fixed"
            else (_ for _ in ()).throw(RuntimeError(f"bad-arg:{text}"))
        ),
        action="read",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    registry.register(
        name="fail_unless_echo_fixed",
        handler=lambda text: (
            f"accepted:{text}"
            if text == "echo:fixed"
            else (_ for _ in ()).throw(RuntimeError(f"bad-output:{text}"))
        ),
        action="read",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    capability_catalog = CapabilityCatalog()
    capability_executor = CapabilityExecutor(capability_catalog)
    ToolCapabilityProvider(registry).register_into(capability_catalog)
    return capability_executor


def _tool_adapter(catalog: CapabilityCatalog) -> CapabilityToolAdapter:
    return CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset("builtin", tuple(sorted(catalog.ids())))],
    )


def _tool_node(node_id: str, tool: str, args: dict) -> DAGNode:
    return DAGNode(
        id=node_id,
        payload=dict(
            type="capability",
            invocation=CapabilityInvocation(
                capability_id=_tool_capability_id(tool),
                kind="tool",
                arguments=args,
                boundary=Boundary(mode="read_only"),
            ),
        ),
    )


def _tool_capability_id(tool_name: str) -> str:
    return tool_name if tool_name.startswith("tool.") else f"tool.{tool_name}"


def _start_node() -> DAGNode:
    return DAGNode(
        id="start",
        payload=dict(type="start"),
    )


def _tool_name_from_capability(
    capability_id: str,
    *,
    tool_adapter: CapabilityToolAdapter,
    enabled_toolsets: tuple[str, ...] = ("builtin",),
) -> str:
    return tool_adapter.function_name_for_capability(
        capability_id,
        enabled_toolsets=enabled_toolsets,
    )


def test_dag_dsl_from_dag_uses_adapter_names_for_selected_capabilities() -> None:
    catalog = CapabilityCatalog()
    catalog.register(
        CapabilityDefinition(
            id="tool.remote_search",
            name="remote_search",
            kind="tool",
        ),
        lambda **_: None,
    )
    tool_adapter = CapabilityToolAdapter(
        catalog,
        toolsets=[CapabilityToolset("selected", ("tool.remote_search",))],
    )
    dag = DAG(
        dag_id="dag_custom",
        task_id="task_custom",
        nodes=[
            _start_node(),
            DAGNode(
                id="search",
                payload=dict(
                    type="capability",
                    invocation=CapabilityInvocation(
                        capability_id="tool.remote_search",
                        kind="tool",
                        arguments={"query": "status"},
                    ),
                ),
            ),
        ],
    )

    dsl = dag_dsl_from_dag(
        dag,
        tool_adapter=tool_adapter,
        enabled_toolsets=("selected",),
    )

    assert dsl == "search = remote_search(query='status')"




def test_llm_dag_agent_compiles_plan_spec_dsl_into_dag() -> None:
    provider = MockProvider([
        ChatResponse(
            content=(
                "task: inspect project\n"
                "list_files = run_command(command=\"dir\", cwd=\".\")\n"
                "show_result = echo(text=\"done\") after list_files\n"
            )
        )
    ])
    dag_loop = dag_loop_for(provider)

    dag_agent = dag_agent_for(dag_loop)
    messages = [dag_agent.system_message]
    requested = run(dag_loop._request_dag(
        task_id="task_real",
        messages=messages,
        user_message=dag_agent.build_request_user_message(
            prompt="What files are here?",
            task_id="task_real",
        ),
    ))
    dag = dag_loop.prepare_for_review(requested)

    assert dag.task_id == "task_real"
    assert [node.id for node in dag.nodes] == ["start", "list_files", "show_result"]
    assert dag.nodes[1].payload.invocation.capability_id == "tool.run_command"
    assert dag.nodes[1].payload.invocation.arguments == {"command": "dir", "cwd": "."}
    assert {(edge.source, edge.target) for edge in dag.edges} == {
        ("start", "list_files"),
        ("list_files", "show_result"),
    }


def test_parse_plan_spec_dsl_accepts_wrapped_output_and_dict_args() -> None:
    plan = parse_plan_spec_dsl(
        """
        PLAN_SPEC
        task: inspect project
        inspect = run_command({"command": "dir", "cwd": "."})
        END_PLAN_SPEC
        """
    )

    assert plan.task == "inspect project"
    assert plan.nodes[0].id == "inspect"
    assert plan.nodes[0].args == {"command": "dir", "cwd": "."}


def test_parse_plan_spec_dsl_accepts_value_expr_args() -> None:
    args = {
        "text": {
            "$expr": {
                "type": "node_output",
                "node_id": "inspect",
                "field": "content",
                "path": [],
            }
        }
    }
    plan = parse_plan_spec_dsl(
        "\n".join([
            "task: summarize",
            'inspect = read_file(path="README.md")',
            f"summarize = echo({args!r}) after inspect",
        ])
    )

    assert plan.nodes[1].args == args


def test_parse_plan_spec_dsl_ignores_thinking_blocks_and_preamble() -> None:
    plan = parse_plan_spec_dsl(
        """
        <think>The user wants repository inspection.</think>
        Here is the requested plan.
        task: inspect project
        inspect = run_command(command="dir", cwd=".")
        """
    )

    assert plan.task == "inspect project"
    assert plan.nodes[0].id == "inspect"
    assert plan.nodes[0].args == {"command": "dir", "cwd": "."}


def test_harness_runtime_auto_approves_low_risk_dag_and_executes() -> None:
    provider = MockProvider([
        ChatResponse(content='inspect = echo(text="ok")'),
        ChatResponse(content="NO_CHANGE"),
    ])
    dag_agent = dag_loop_for(provider)
    executor = DAGExecutor(capability_executor=make_capability_executor())
    runtime = runtime_for(dag_agent_loop=dag_agent, executor=executor)

    loop_outcome = run(runtime.dag_agent.run("Do a safe task", task_id="task_1", review_level="fast"))
    result = loop_outcome.state.trace
    assert result is not None

    assert loop_outcome.state.status == "completed"
    assert result.status == "completed"
    assert loop_outcome.state.dag is not None
    assert loop_outcome.state.dag.status == "completed"
    assert dag_node_trace(result, "inspect").output == "echo:ok"


def test_harness_runtime_careful_reviews_initial_dag() -> None:
    provider = MockProvider([ChatResponse(content='inspect = echo(text="ok")')])
    dag_agent = dag_loop_for(provider)
    executor = DAGExecutor(capability_executor=make_capability_executor())
    runtime = runtime_for(dag_agent_loop=dag_agent, executor=executor)

    loop_outcome = run(runtime.dag_agent.run("Do a reviewed task", task_id="task_1", review_level="careful"))

    assert loop_outcome.state.status == "awaiting_review"
    assert loop_outcome.state.trace is None
    assert loop_outcome.state.dag is not None
    assert loop_outcome.state.dag.status == "review_required"
    assert loop_outcome.state.pending_review is not None
    assert loop_outcome.state.pending_review.kind == "initial_dag"


def test_harness_runtime_dag_review_approval_authorizes_node_boundaries() -> None:
    provider = MockProvider([ChatResponse(content="Boundary-approved DAG completed.")])
    executor = DAGExecutor(capability_executor=make_capability_executor())
    runtime = runtime_for(dag_agent_loop=dag_loop_for(provider, executor), executor=executor)
    record = dag_state(
        task_id="task_boundary_review",
        user_request="Write the reviewed file",
        dag=DAG(dag_id="dag_boundary_review", task_id="task_boundary_review", nodes=[]),
        review_level="careful",
    )
    record.internal_messages = [{"role": "user", "content": "Write the reviewed file"}]
    proposed_node = _tool_node(
        "write_reviewed",
        "write_file",
        {"path": "blocked/notes.md", "content": "hi"},
    )
    proposed_node.payload.invocation.boundary = Boundary(mode="read_only", allowed_paths=["allowed"])
    proposed_node.payload.invocation.risk = "medium"

    runtime.dag_agent.loop._apply_replan(
        record,
        DAG(
            dag_id="dag_boundary_review",
            task_id="task_boundary_review",
            nodes=[proposed_node],
        ),
    )

    assert record.pending_review is not None
    result = run(
        runtime.dag_agent.resume_review(
            record,
            dag=record.pending_review.proposed_dag,
            approved=True,
        )
    )

    assert result is not None
    assert result.state.status == "completed"
    assert dag_node_trace(result.state.trace, "write_reviewed").output.endswith("blocked/notes.md:hi")


def test_harness_runtime_fast_replan_does_not_authorize_node_boundaries() -> None:
    provider = MockProvider([])
    executor = DAGExecutor(capability_executor=make_capability_executor())
    dag_loop = dag_loop_for(provider, executor)
    record = dag_state(
        task_id="task_fast_boundary",
        user_request="Write without review",
        dag=DAG(dag_id="dag_fast_boundary", task_id="task_fast_boundary", nodes=[]),
        review_level="fast",
    )
    proposed_node = _tool_node(
        "write_unreviewed",
        "write_file",
        {"path": "blocked/notes.md", "content": "hi"},
    )
    proposed_node.payload.invocation.boundary = Boundary(mode="read_only", allowed_paths=["allowed"])
    proposed_node.payload.invocation.risk = "medium"

    dag_loop._apply_replan(
        record,
        DAG(
            dag_id="dag_fast_boundary",
            task_id="task_fast_boundary",
            nodes=[proposed_node],
        ),
    )

    assert record.pending_review is None
    result = run(dag_loop.execute(record, replan=False))

    assert result is not None
    assert result.status == "failed"
    assert dag_node_trace(result, "write_unreviewed").status == "failed"


def test_harness_runtime_executes_layers_with_no_change_replan() -> None:
    """When replan returns NO_CHANGE, layers execute with original args."""
    initial = DAG(
        dag_id="dag_replan",
        task_id="task_replan",
        status="approved",
        nodes=[
            _tool_node("inspect", "echo", {"text": "observed"}),
            _tool_node("answer", "echo", {"text": "old"}),
        ],
        edges=[DAGEdge(source="inspect", target="answer")],
    )
    runtime = runtime_for(
        dag_agent_loop=dag_loop_for(MockProvider([
            ChatResponse(content="NO_CHANGE"),
            ChatResponse(content="Both nodes completed."),
        ])),
        executor=DAGExecutor(capability_executor=make_capability_executor()),
    )
    prepared = runtime.dag_agent.loop.prepare_for_review(initial)
    record = dag_state(
        task_id="task_replan",
        user_request="Use observation downstream",
        dag=prepared,
        review_level="fast",
    )
    runtime.runs[record.run_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.status == "completed"
    assert dag_node_trace(result, "inspect").output == "echo:observed"
    assert dag_node_trace(result, "answer").output == "echo:old"


def test_harness_runtime_replan_adjusts_params_after_success() -> None:
    """When replan returns adjusted PlanSpec DSL, pending node args are updated."""
    initial = DAG(
        dag_id="dag_l2",
        task_id="task_l2",
        status="approved",
        nodes=[
            _tool_node("inspect", "echo", {"text": "discovered_path"}),
            _tool_node("answer", "echo", {"text": "placeholder"}),
        ],
        edges=[DAGEdge(source="inspect", target="answer")],
    )
    adjusted_dsl = (
        'task: adjusted\n'
        'inspect = echo(text="discovered_path")\n'
        'answer = echo(text="adjusted_value") after inspect\n'
    )
    runtime = runtime_for(
        dag_agent_loop=dag_loop_for(MockProvider([
            ChatResponse(content=adjusted_dsl),
            ChatResponse(content="NO_CHANGE"),
            ChatResponse(content="Adjusted and completed."),
        ])),
        executor=DAGExecutor(capability_executor=make_capability_executor()),
    )
    prepared = runtime.dag_agent.loop.prepare_for_review(initial)
    record = dag_state(
        task_id="task_l2",
        user_request="Adjust downstream based on observation",
        dag=prepared,
        review_level="fast",
    )
    runtime.runs[record.run_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.status == "completed"
    assert dag_node_trace(result, "answer").output == "echo:adjusted_value"
    request = runtime.dag_agent.loop.provider.requests[0]["messages"][-1]["content"]
    assert "Node executions:" in request
    assert "- node: inspect" in request
    assert "  tool: tool.echo" in request
    assert '  args: {"text": "discovered_path"}' in request
    assert "  status: completed" in request
    assert "  content:" in request
    assert "echo:discovered_path" in request


def test_harness_runtime_careful_reviews_replan_changes() -> None:
    initial = DAG(
        dag_id="dag_l2_review",
        task_id="task_l2_review",
        status="approved",
        nodes=[
            _tool_node("inspect", "echo", {"text": "discovered_path"}),
            _tool_node("answer", "echo", {"text": "placeholder"}),
        ],
        edges=[DAGEdge(source="inspect", target="answer")],
    )
    adjusted_dsl = (
        'task: adjusted\n'
        'inspect = echo(text="discovered_path")\n'
        'answer = echo(text="adjusted_value") after inspect\n'
    )
    runtime = runtime_for(
        dag_agent_loop=dag_loop_for(MockProvider([ChatResponse(content=adjusted_dsl)])),
        executor=DAGExecutor(capability_executor=make_capability_executor()),
    )
    prepared = runtime.dag_agent.loop.prepare_for_review(initial)
    record = dag_state(
        task_id="task_l2_review",
        user_request="Adjust downstream based on observation",
        dag=prepared,
        review_level="careful",
    )
    runtime.runs[record.run_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.status != "completed"
    assert record.dag.status == "review_required"
    assert record.pending_review is not None
    assert record.pending_review.kind == "dag_replan"
    assert record.pending_review.proposed_dag.nodes[-1].payload.invocation.arguments == {"text": "adjusted_value"}


def test_harness_runtime_replans_after_tool_failure() -> None:
    capability_executor = make_capability_executor()
    tool_adapter = _tool_adapter(capability_executor.catalog)
    executor = DAGExecutor(capability_executor=capability_executor)
    initial = DAG(
        dag_id="dag_failure_replan",
        task_id="task_failure_replan",
        status="approved",
        nodes=[
            _tool_node("try_bad_tool", "fail_tool", {"text": "boom"}),
        ],
        edges=[],
    )
    replacement = DAG(
        dag_id="replacement",
        task_id="task_failure_replan",
        nodes=[
            _tool_node("fallback", "echo", {"text": "recovered"}),
        ],
        edges=[],
    )
    runtime = runtime_for(
        dag_agent_loop=dag_loop_for(
            MockProvider([
                ChatResponse(content=dag_dsl_from_dag(replacement, tool_adapter=tool_adapter)),
                ChatResponse(content="Recovery complete."),
            ]),
            executor=executor,
        ),
        executor=executor,
    )
    prepared = runtime.dag_agent.loop.prepare_for_review(initial)
    record = dag_state(
        task_id="task_failure_replan",
        user_request="Recover from failure",
        dag=prepared,
        review_level="fast",
    )
    runtime.runs[record.run_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.status == "completed"
    assert dag_node_trace(result, "fallback").output == "echo:recovered"
    request = runtime.dag_agent.loop.provider.requests[0]["messages"][-1]["content"]
    assert "Node executions:" in request
    assert "- node: try_bad_tool" in request
    assert "  tool: tool.fail_tool" in request
    assert '  args: {"text": "boom"}' in request
    assert "  status: failed" in request
    assert "  content:" in request
    assert "failed:boom" in request
    assert "User request:" not in request


def test_replan_sees_prior_planning_output_in_agent_thread() -> None:
    """Replan LLM call includes the initial planning exchange in the agent thread."""
    initial_dsl = (
        'task: initial\n'
        'inspect = echo(text="hello")\n'
        'answer = echo(text="placeholder") after inspect\n'
    )
    adjusted_dsl = (
        'task: adjusted\n'
        'inspect = echo(text="hello")\n'
        'answer = echo(text="fixed") after inspect\n'
    )
    provider = MockProvider([
        ChatResponse(content=initial_dsl),
        ChatResponse(content=adjusted_dsl),
        ChatResponse(content="NO_CHANGE"),
        ChatResponse(content="All steps completed."),
    ])
    dag_agent = dag_loop_for(provider)
    executor = DAGExecutor(capability_executor=make_capability_executor())
    runtime = runtime_for(dag_agent_loop=dag_agent, executor=executor)

    result = run(runtime.dag_agent.run("Do two steps", task_id="task_dm", review_level="fast"))
    replan_messages = provider.requests[1]["messages"]
    assert [message["role"] for message in replan_messages[:4]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert initial_dsl in replan_messages[2]["content"]
    assert "DAG observation" in replan_messages[3]["content"]
    assert result.state.trace is not None
    assert result.state.trace.status == "completed"
    assert result.state.dag is not None
    assert result.state.dag.status == "completed"


def test_harness_runtime_marks_dag_failed_when_replan_is_unavailable_after_tool_error() -> None:
    initial = DAG(
        dag_id="dag_failure_needs_review",
        task_id="task_failure_needs_review",
        status="approved",
        nodes=[
            _tool_node("try_bad_tool", "fail_tool", {"text": "boom"}),
        ],
        edges=[],
    )
    runtime = runtime_for(
        dag_agent_loop=dag_loop_for(MockProvider([])),
        executor=DAGExecutor(capability_executor=make_capability_executor()),
    )
    prepared = runtime.dag_agent.loop.prepare_for_review(initial)
    record = dag_state(
        task_id="task_failure_needs_review",
        user_request="Recover from failure",
        dag=prepared,
        review_level="fast",
    )
    runtime.runs[record.run_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.status != "completed"
    assert record.dag.status == "failed"
    assert record.dag.nodes[0].status == "failed"
    assert record.pending_review is None


def test_harness_runtime_preserves_parallel_successes_when_sibling_fails() -> None:
    initial = DAG(
        dag_id="dag_parallel_failure",
        task_id="task_parallel_failure",
        status="approved",
        nodes=[
            _start_node(),
            _tool_node("ok", "echo", {"text": "kept"}),
            _tool_node("bad_a", "fail_tool", {"text": "a"}),
            _tool_node("bad_b", "fail_tool", {"text": "b"}),
        ],
        edges=[
            DAGEdge(source="start", target="ok"),
            DAGEdge(source="start", target="bad_a"),
            DAGEdge(source="start", target="bad_b"),
        ],
    )
    runtime = runtime_for(
        dag_agent_loop=dag_loop_for(MockProvider([])),
        executor=DAGExecutor(capability_executor=make_capability_executor()),
    )
    prepared = runtime.dag_agent.loop.prepare_for_review(initial)
    record = dag_state(
        task_id="task_parallel_failure",
        user_request="Recover from parallel failure",
        dag=prepared,
        review_level="fast",
    )
    runtime.runs[record.run_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.status != "completed"
    assert dag_node_trace(result, "ok").output == "echo:kept"
    assert dag_node_trace(record.trace, "ok").output == "echo:kept"
    node_statuses = {node.id: node.status for node in record.dag.nodes}
    assert node_statuses["ok"] == "completed"
    assert node_statuses["bad_a"] == "failed"
    assert record.pending_review is None
    assert record.dag.status == "failed"


def test_harness_runtime_fails_when_replan_is_unavailable() -> None:
    initial = DAG(
        dag_id="dag_failure_requires_edit",
        task_id="task_failure_requires_edit",
        status="approved",
        nodes=[
            _tool_node("try_bad_tool", "fail_tool", {"text": "boom"}),
        ],
        edges=[],
    )
    runtime = runtime_for(
        dag_agent_loop=dag_loop_for(MockProvider([])),
        executor=DAGExecutor(capability_executor=make_capability_executor()),
    )
    prepared = runtime.dag_agent.loop.prepare_for_review(initial)
    record = dag_state(
        task_id="task_failure_requires_edit",
        user_request="Recover from failure",
        dag=prepared,
        review_level="fast",
    )
    runtime.runs[record.run_id] = record

    result = run(runtime.dag_agent.execute(record))
    assert result.status != "completed"
    assert record.pending_review is None
    assert record.dag.status == "failed"


def test_harness_runtime_pauses_when_dag_agent_fails_after_tool_error() -> None:
    initial = DAG(
        dag_id="dag_agent_failure_review",
        task_id="task_dag_agent_failure_review",
        status="approved",
        nodes=[
            _tool_node("try_bad_tool", "fail_tool", {"text": "boom"}),
        ],
        edges=[],
    )
    runtime = runtime_for(
        dag_agent_loop=dag_loop_for(MockProvider([])),
        executor=DAGExecutor(capability_executor=make_capability_executor()),
    )
    prepared = runtime.dag_agent.loop.prepare_for_review(initial)
    record = dag_state(
        task_id="task_dag_agent_failure_review",
        user_request="Recover from failure",
        dag=prepared,
        review_level="fast",
    )
    runtime.runs[record.run_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.status != "completed"
    assert record.pending_review is None
    assert record.dag.status == "failed"


def test_harness_runtime_ignores_stale_failed_trace_nodes_after_replan() -> None:
    capability_executor = make_capability_executor()
    tool_adapter = _tool_adapter(capability_executor.catalog)
    executor = DAGExecutor(capability_executor=capability_executor)
    initial = DAG(
        dag_id="dag_stale_trace",
        task_id="task_stale_trace",
        status="approved",
        nodes=[
            _tool_node("search_config", "fail_tool", {"text": "old"}),
        ],
        edges=[],
    )
    replacement = DAG(
        dag_id="replacement",
        task_id="task_stale_trace",
        nodes=[
            _tool_node("current_failure", "fail_tool", {"text": "current"}),
        ],
        edges=[],
    )
    runtime = runtime_for(
        dag_agent_loop=dag_loop_for(
            MockProvider([ChatResponse(content=dag_dsl_from_dag(replacement, tool_adapter=tool_adapter))]),
            executor=executor,
        ),
        executor=executor,
    )
    prepared = runtime.dag_agent.loop.prepare_for_review(initial)
    record = dag_state(
        task_id="task_stale_trace",
        user_request="Recover from stale failed node traces",
        dag=prepared,
        review_level="fast",
    )
    runtime.runs[record.run_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.status != "completed"
    assert record.pending_review is None
    assert "search_config" not in {node.id for node in record.dag.nodes}
    assert {node.id: node.status for node in record.dag.nodes}["current_failure"] == "failed"


def test_harness_runtime_patches_failed_node_and_retries() -> None:
    """When a node fails but replan patches its args, execution retries with new args."""
    capability_executor = make_capability_executor()
    tool_adapter = _tool_adapter(capability_executor.catalog)
    executor = DAGExecutor(capability_executor=capability_executor)
    initial = DAG(
        dag_id="dag_patch_retry",
        task_id="task_patch_retry",
        status="approved",
        nodes=[
            _tool_node("fragile", "fail_unless_fixed", {"text": "bad"}),
        ],
        edges=[],
    )
    replacement = DAG(
        dag_id="replacement",
        task_id="task_patch_retry",
        nodes=[
            _tool_node("fragile", "fail_unless_fixed", {"text": "fixed"}),
        ],
        edges=[],
    )
    runtime = runtime_for(
        dag_agent_loop=dag_loop_for(
            MockProvider([
                ChatResponse(content=dag_dsl_from_dag(replacement, tool_adapter=tool_adapter)),
                ChatResponse(content="Patched and completed."),
            ]),
            executor=executor,
        ),
        executor=executor,
    )
    prepared = runtime.dag_agent.loop.prepare_for_review(initial)
    record = dag_state(
        task_id="task_patch_retry",
        user_request="Patch and retry",
        dag=prepared,
        review_level="fast",
    )
    runtime.runs[record.run_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.status == "completed"
    assert dag_node_trace(result, "fragile").output == "fixed-ok"


def test_harness_runtime_edge_only_replan_invalidates_downstream_results() -> None:
    initial = DAG(
        dag_id="dag_edge_only_replan",
        task_id="task_edge_only_replan",
        status="approved",
        nodes=[
            _tool_node("source", "echo", {"text": "source"}),
            _tool_node("sink", "echo", {"text": "same"}),
        ],
        edges=[DAGEdge(source="source", target="sink")],
    )
    replacement = DAG(
        dag_id="replacement",
        task_id="task_edge_only_replan",
        nodes=[
            _tool_node("source", "echo", {"text": "source"}),
            _tool_node("middle", "echo", {"text": "middle"}),
            _tool_node("sink", "echo", {"text": "same"}),
        ],
        edges=[
            DAGEdge(source="source", target="middle"),
            DAGEdge(source="middle", target="sink"),
        ],
    )
    loop = dag_loop_for(MockProvider([]))
    prepared = loop.prepare_for_review(initial)
    record = dag_state(
        task_id="task_edge_only_replan",
        user_request="Change dependencies only",
        dag=prepared,
        review_level="fast",
    )
    record.trace = trace_with_completed_nodes(
        "task_edge_only_replan",
        {"source": "echo:source", "sink": "echo:same"},
    )

    loop._apply_replan(record, replacement)

    assert record.trace is not None
    assert "sink" not in {
        child.ref.get("node_id")
        for child in record.trace.root.children
        if child.kind == "dag_node"
    }


def test_harness_runtime_pauses_for_permission_and_resumes_after_approval() -> None:
    pass
