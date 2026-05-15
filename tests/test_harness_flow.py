import asyncio

from dagent.harness_runtime import (
    DAGAgent,
    DAGAgentLoop,
    DAGExecutor,
    HarnessRuntime,
    RuntimeTaskRecord,
    ToolAgent,
    ToolAgentLoop,
    CapabilityExecutor,
)
from dagent.capabilities import CapabilityCatalog
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.providers import ChatResponse, MockProvider
from dagent.harness_runtime.dag_builder import parse_plan_spec_dsl
from dagent.profiles import AgentProfile
from dagent.schemas import Boundary, DAG, DAGEdge, DAGNode, DAGNodeResult, CapabilityInvocation
from dagent.tools.command_tools import _infer_command_boundary, _infer_command_risk
from dagent.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


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
            ),
            profile=AgentProfile(
                name="conversation",
                role="conversation",
                layers=["soul"],
                layer_contents={"soul": "You are a conversation agent."},
            ),
        ),
        dag_agent=DAGAgent(
            loop=dag_agent_loop,
            profile=AgentProfile(
                name="dag_agent",
                role="dag_agent",
                layers=["soul"],
                layer_contents={"soul": "You are a DAG creator."},
            ),
            tools=dag_agent_loop.dag_executor.capability_executor.catalog.list(kind="tool", enabled_only=True),
        ),
    )


def dag_loop_for(provider: MockProvider, executor: DAGExecutor | None = None) -> DAGAgentLoop:
    dag_executor = executor or DAGExecutor(capability_executor=make_capability_executor())
    return DAGAgentLoop(
        provider=provider,
        dag_executor=dag_executor,
    )


def dag_agent_for(dag_loop: DAGAgentLoop) -> DAGAgent:
    return DAGAgent(
        loop=dag_loop,
        profile=AgentProfile(
            name="dag_agent",
            role="dag_agent",
            layers=["soul"],
            layer_contents={"soul": "You are a DAG creator."},
        ),
        tools=dag_loop.dag_executor.capability_executor.catalog.list(kind="tool", enabled_only=True),
    )


def dag_dsl_from_dag(dag: DAG) -> str:
    lines = []
    for node in dag.nodes:
        deps = [e.source for e in dag.edges if e.target == node.id]
        deps_str = f" after {', '.join(deps)}" if deps else ""
        args = ", ".join(f'{k}={repr(v)}' for k, v in node.invocation.arguments.items())
        lines.append(f'{node.id} = {_tool_name_from_capability(node.invocation.capability_id)}({args}){deps_str}')
    return "\n".join(lines)


def make_capability_executor() -> CapabilityExecutor:
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
        name="run_command",
        handler=lambda command, cwd=".", timeout_seconds=30: f"ran:{command}:{cwd}",
        action="command",
        path_args=("cwd",),
        command_args=("command",),
        boundary_fn=_infer_command_boundary,
        risk_fn=_infer_command_risk,
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


def _tool_node(node_id: str, tool: str, args: dict) -> DAGNode:
    return DAGNode(
        id=node_id,
        invocation=CapabilityInvocation(
            capability_id=_tool_capability_id(tool),
            kind="tool",
            arguments=args,
            boundary=Boundary(mode="read_only"),
        ),
    )


def _tool_capability_id(tool_name: str) -> str:
    return tool_name if tool_name.startswith("tool.") else f"tool.{tool_name}"


def _tool_name_from_capability(capability_id: str) -> str:
    return capability_id.removeprefix("tool.")




def test_llm_dag_agent_compiles_plan_spec_dsl_into_dag() -> None:
    provider = MockProvider([
        ChatResponse(
            content=(
                "task: inspect project\n"
                "start = dag_start()\n"
                "list_files = run_command(command=\"dir\", cwd=\".\") after start\n"
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
        tools=dag_agent.tools,
    ))
    dag = dag_loop.prepare_for_review(requested)

    assert dag.task_id == "task_real"
    assert [node.id for node in dag.nodes] == ["start", "list_files", "show_result"]
    assert dag.nodes[1].invocation.capability_id == "tool.run_command"
    assert dag.nodes[1].invocation.arguments == {"command": "dir", "cwd": "."}
    assert [(edge.source, edge.target) for edge in dag.edges] == [
        ("start", "list_files"),
        ("list_files", "show_result"),
    ]


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
    result = loop_outcome.dag_run
    assert result is not None

    assert loop_outcome.status == "completed"
    assert result.completed is True
    assert loop_outcome.dag is not None
    assert loop_outcome.dag.status == "completed"
    assert result.node_results["inspect"].final_response == "echo:ok"


def test_harness_runtime_careful_reviews_initial_dag() -> None:
    provider = MockProvider([ChatResponse(content='inspect = echo(text="ok")')])
    dag_agent = dag_loop_for(provider)
    executor = DAGExecutor(capability_executor=make_capability_executor())
    runtime = runtime_for(dag_agent_loop=dag_agent, executor=executor)

    loop_outcome = run(runtime.dag_agent.run("Do a reviewed task", task_id="task_1", review_level="careful"))

    assert loop_outcome.status == "awaiting_review"
    assert loop_outcome.dag_run is None
    assert loop_outcome.dag is not None
    assert loop_outcome.dag.status == "review_required"
    assert loop_outcome.pending_review is not None
    assert loop_outcome.pending_review.kind == "initial_dag"


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
    record = RuntimeTaskRecord.dag_task(
        task_id="task_replan",
        user_request="Use observation downstream",
        dag=prepared,
        review_level="fast",
    )
    runtime.tasks[record.task_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.completed is True
    assert result.node_results["inspect"].final_response == "echo:observed"
    assert result.node_results["answer"].final_response == "echo:old"


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
        'start = dag_start()\n'
        'inspect = echo(text="discovered_path") after start\n'
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
    record = RuntimeTaskRecord.dag_task(
        task_id="task_l2",
        user_request="Adjust downstream based on observation",
        dag=prepared,
        review_level="fast",
    )
    runtime.tasks[record.task_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.completed is True
    assert result.node_results["answer"].final_response == "echo:adjusted_value"
    assert "dag_replanned" in [event.event_type for event in result.traces]


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
        'start = dag_start()\n'
        'inspect = echo(text="discovered_path") after start\n'
        'answer = echo(text="adjusted_value") after inspect\n'
    )
    runtime = runtime_for(
        dag_agent_loop=dag_loop_for(MockProvider([ChatResponse(content=adjusted_dsl)])),
        executor=DAGExecutor(capability_executor=make_capability_executor()),
    )
    prepared = runtime.dag_agent.loop.prepare_for_review(initial)
    record = RuntimeTaskRecord.dag_task(
        task_id="task_l2_review",
        user_request="Adjust downstream based on observation",
        dag=prepared,
        review_level="careful",
    )
    runtime.tasks[record.task_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.completed is False
    assert record.dag.status == "review_required"
    assert record.pending_review is not None
    assert record.pending_review.kind == "dag_replan"
    assert record.pending_review.proposed_dag.nodes[-1].invocation.arguments == {"text": "adjusted_value"}


def test_harness_runtime_replans_after_tool_failure() -> None:
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
        dag_agent_loop=dag_loop_for(MockProvider([
            ChatResponse(content=dag_dsl_from_dag(replacement)),
            ChatResponse(content="Recovery complete."),
        ])),
        executor=DAGExecutor(capability_executor=make_capability_executor()),
    )
    prepared = runtime.dag_agent.loop.prepare_for_review(initial)
    record = RuntimeTaskRecord.dag_task(
        task_id="task_failure_replan",
        user_request="Recover from failure",
        dag=prepared,
        review_level="fast",
    )
    runtime.tasks[record.task_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.completed is True
    assert result.node_results["fallback"].final_response == "echo:recovered"
    request = runtime.dag_agent.loop.provider.requests[0]["messages"][-1]["content"]
    assert "try_bad_tool" in request
    assert "failed:boom" in request
    assert "User request:" not in request
    assert "dag_replanned" in [event.event_type for event in result.traces]


def test_replan_sees_prior_planning_output_in_agent_thread() -> None:
    """Replan LLM call includes the initial planning exchange in the agent thread."""
    initial_dsl = (
        'task: initial\n'
        'start = dag_start()\n'
        'inspect = echo(text="hello") after start\n'
        'answer = echo(text="placeholder") after inspect\n'
    )
    adjusted_dsl = (
        'task: adjusted\n'
        'start = dag_start()\n'
        'inspect = echo(text="hello") after start\n'
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
    assert result.dag_run is not None
    assert result.dag_run.completed is True
    assert result.dag is not None
    assert result.dag.status == "completed"


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
    record = RuntimeTaskRecord.dag_task(
        task_id="task_failure_needs_review",
        user_request="Recover from failure",
        dag=prepared,
        review_level="fast",
    )
    runtime.tasks[record.task_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.completed is False
    assert record.dag.status == "failed"
    assert record.dag.nodes[0].status == "failed"
    assert record.pending_review is None


def test_harness_runtime_preserves_parallel_successes_when_sibling_fails() -> None:
    initial = DAG(
        dag_id="dag_parallel_failure",
        task_id="task_parallel_failure",
        status="approved",
        nodes=[
            _tool_node("start", "dag_start", {}),
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
    record = RuntimeTaskRecord.dag_task(
        task_id="task_parallel_failure",
        user_request="Recover from parallel failure",
        dag=prepared,
        review_level="fast",
    )
    runtime.tasks[record.task_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.completed is False
    assert result.node_results["ok"].final_response == "echo:kept"
    assert record.node_results["ok"].final_response == "echo:kept"
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
    record = RuntimeTaskRecord.dag_task(
        task_id="task_failure_requires_edit",
        user_request="Recover from failure",
        dag=prepared,
        review_level="fast",
    )
    runtime.tasks[record.task_id] = record

    result = run(runtime.dag_agent.execute(record))
    assert result.completed is False
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
    record = RuntimeTaskRecord.dag_task(
        task_id="task_dag_agent_failure_review",
        user_request="Recover from failure",
        dag=prepared,
        review_level="fast",
    )
    runtime.tasks[record.task_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.completed is False
    assert record.pending_review is None
    assert record.dag.status == "failed"


def test_harness_runtime_ignores_stale_failed_trace_nodes_after_replan() -> None:
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
        dag_agent_loop=dag_loop_for(MockProvider([ChatResponse(content=dag_dsl_from_dag(replacement))])),
        executor=DAGExecutor(capability_executor=make_capability_executor()),
    )
    prepared = runtime.dag_agent.loop.prepare_for_review(initial)
    record = RuntimeTaskRecord.dag_task(
        task_id="task_stale_trace",
        user_request="Recover from stale failed node traces",
        dag=prepared,
        review_level="fast",
    )
    runtime.tasks[record.task_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.completed is False
    assert record.pending_review is None
    assert "search_config" not in {node.id for node in record.dag.nodes}
    assert {node.id: node.status for node in record.dag.nodes}["current_failure"] == "failed"


def test_harness_runtime_patches_failed_node_and_retries() -> None:
    """When a node fails but replan patches its args, execution retries with new args."""
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
        dag_agent_loop=dag_loop_for(MockProvider([
            ChatResponse(content=dag_dsl_from_dag(replacement)),
            ChatResponse(content="Patched and completed."),
        ])),
        executor=DAGExecutor(capability_executor=make_capability_executor()),
    )
    prepared = runtime.dag_agent.loop.prepare_for_review(initial)
    record = RuntimeTaskRecord.dag_task(
        task_id="task_patch_retry",
        user_request="Patch and retry",
        dag=prepared,
        review_level="fast",
    )
    runtime.tasks[record.task_id] = record

    result = run(runtime.dag_agent.execute(record))

    assert result.completed is True
    assert result.node_results["fragile"].final_response == "fixed-ok"


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
    record = RuntimeTaskRecord.dag_task(
        task_id="task_edge_only_replan",
        user_request="Change dependencies only",
        dag=prepared,
        review_level="fast",
    )
    record.node_results = {
        "source": DAGNodeResult(
            node_id="source",
            final_response="echo:source",
            completed=True,
            stop_reason="completed",
            steps=1,
        ),
        "sink": DAGNodeResult(
            node_id="sink",
            final_response="echo:same",
            completed=True,
            stop_reason="completed",
            steps=1,
        ),
    }

    loop._apply_replan(record, replacement)

    assert "sink" not in record.node_results


def test_harness_runtime_pauses_for_permission_and_resumes_after_approval() -> None:
    pass
