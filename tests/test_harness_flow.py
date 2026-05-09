import asyncio
import json

import pytest

from dagent.harness_runtime import (
    DAGCreationError,
    DAGExecutionError,
    DAGExecutor,
    HarnessRuntime,
    LLMDAGAgent,
    NodeExecutionResult,
    TaskRecord,
)
from dagent.harness_runtime.dag_validation import DAGValidationError
from dagent.providers import ChatResponse, MockProvider
from dagent.harness_runtime import AgentLoopResult
from dagent.harness_runtime.dag_agent import parse_plan_spec_dsl
from dagent.profiles import AgentProfile
from dagent.schemas import Boundary, DAG, DAGEdge, DAGNode
from dagent.tools.boundary import BoundaryViolation
from dagent.tools.command_tools import _infer_command_boundary, _infer_command_risk
from dagent.tools.executor import ToolExecutor
from dagent.tools.registry import Tool
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


class EmptySummaryLoop:
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
            final_response="",
            messages=[],
            steps=1,
            completed=True,
            stop_reason="completed",
        )


class PermissionThenCompletingLoop:
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
        if self.calls == 1:
            raise BoundaryViolation(
                "Command 'python --version' is not allowed.",
                command="python --version",
            )
        return AgentLoopResult(
            final_response="node complete after permission",
            messages=[],
            steps=1,
            completed=True,
            stop_reason="completed",
        )


def run(coro):
    return asyncio.run(coro)


def runtime_for(
    *,
    dag_agent: LLMDAGAgent,
    executor: DAGExecutor,
    agent_loop=None,
    max_replans: int = 3,
    max_node_retries: int = 2,
) -> HarnessRuntime:
    return HarnessRuntime(
        agent_loop=agent_loop or CompletingLoop(),
        dag_agent=dag_agent,
        dag_executor=executor,
        conversation_profile=AgentProfile(
            name="conversation",
            role="conversation",
            layers=["soul"],
            layer_contents={"soul": "You are a conversation agent."},
        ),
        max_replans=max_replans,
        max_node_retries=max_node_retries,
    )


def dag_agent_json(*, tools: list[str] | None = None, risk: str = "low") -> str:
    tool = (tools or ["echo"])[0]
    args = _default_args_for_tool(tool)
    boundary = _default_boundary_for_tool(tool)
    return json.dumps(
        {
            "dag_id": "dag_real",
            "task_id": "will_be_overridden",
            "version": 1,
            "status": "draft",
            "nodes": [
                {
                    "id": "inspect",
                    "tool": tool,
                    "args": args,
                    "boundary": boundary,
                    "risk": risk,
                }
            ],
            "edges": [],
        }
    )


def dag_agent_json_with_boundary_modes(modes: list[str]) -> str:
    payload = json.loads(dag_agent_json())
    payload["nodes"] = [
        {
            **payload["nodes"][0],
            "id": f"node_{index}",
            "boundary": {
                **payload["nodes"][0]["boundary"],
                "mode": mode,
            },
        }
        for index, mode in enumerate(modes)
    ]
    return json.dumps(payload)


def plan_spec_json() -> str:
    return json.dumps(
        {
            "task": "List files in the current directory",
            "nodes": [
                {
                    "id": "list_files",
                    "tool": "run_command",
                    "args": {"command": "dir", "cwd": "."},
                }
            ],
        }
    )


def dag_json(dag: DAG) -> str:
    return json.dumps(dag.model_dump(mode="json"))


def _default_args_for_tool(tool: str) -> dict:
    if tool == "write_file":
        return {"path": "notes.md", "content": "hi"}
    if tool == "run_command":
        return {"command": "python --version", "cwd": "."}
    return {"text": "ok"}


def _default_boundary_for_tool(tool: str) -> dict:
    if tool == "write_file":
        return {
            "mode": "write_limited",
            "allowed_paths": ["notes.md"],
            "allowed_commands": [],
        }
    if tool == "run_command":
        return {
            "mode": "read_only",
            "allowed_paths": ["."],
            "allowed_commands": [],
        }
    return {
        "mode": "read_only",
        "allowed_paths": [],
        "allowed_commands": [],
    }


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
    return ToolExecutor(registry)


def _tool_node(node_id: str, tool: str, args: dict) -> DAGNode:
    return DAGNode(
        id=node_id,
        tool=tool,
        args=args,
        boundary=Boundary(mode="read_only"),
    )


def plan_spec_json_without_tool() -> str:
    return json.dumps(
        {
            "task": "当前目录有哪些文件？",
            "nodes": [
                {
                    "id": "list_files",
                    "depends_on": [],
                }
            ],
        }
    )


def full_dag_json_without_tool_node() -> str:
    payload = json.loads(dag_agent_json())
    payload["nodes"][0].pop("tool", None)
    payload["nodes"][0].pop("args", None)
    payload["nodes"][0]["tools"] = []
    return json.dumps(payload)


def test_llm_dag_agent_parses_model_json_into_dag() -> None:
    provider = MockProvider([ChatResponse(content=dag_agent_json())])
    dag_agent = LLMDAGAgent(
        provider,
        tools=[
            Tool(
                name="read_file",
                handler=lambda: "",
                action="read",
                description="Read files.",
            )
        ],
    )

    dag = run(dag_agent.aplan("Plan something", task_id="task_real"))

    assert dag.dag_id == "dag_real"
    assert dag.task_id == "task_real"
    assert dag.nodes[0].id == "inspect"
    assert provider.requests[0]["messages"][0]["role"] == "system"
    assert "dag_agent" in provider.requests[0]["messages"][0]["content"]
    assert "read_file: Read files." in provider.requests[0]["messages"][0]["content"]
    assert "task_real" in provider.requests[0]["messages"][1]["content"]


def test_llm_dag_agent_compiles_compact_plan_spec_into_dag() -> None:
    provider = MockProvider([ChatResponse(content=plan_spec_json())])
    dag_agent = LLMDAGAgent(provider, tools=make_tool_executor().registry.all_tools())

    dag = run(dag_agent.aplan("What files are here?", task_id="task_real"))

    assert dag.task_id == "task_real"
    assert dag.nodes[0].id == "list_files"
    assert dag.nodes[0].tool == "run_command"
    assert dag.nodes[0].args == {"command": "dir", "cwd": "."}
    assert dag.nodes[0].boundary.mode == "read_only"
    assert dag.nodes[0].boundary.allowed_paths == ["."]
    assert dag.nodes[0].boundary.allowed_commands == []


def test_llm_dag_agent_compiles_plan_spec_dsl_into_dag() -> None:
    provider = MockProvider([
        ChatResponse(
            content=(
                "task: inspect project\n"
                "start = dag_start()\n"
                "list_files = run_command(command=\"dir\", cwd=\".\") after start\n"
                "read_readme = read_file(path=\"README.md\") after list_files\n"
            )
        )
    ])
    dag_agent = LLMDAGAgent(provider, tools=make_tool_executor().registry.all_tools())

    dag = run(dag_agent.aplan("What files are here?", task_id="task_real"))

    assert dag.task_id == "task_real"
    assert [node.id for node in dag.nodes] == ["start", "list_files", "read_readme"]
    assert dag.nodes[1].tool == "run_command"
    assert dag.nodes[1].args == {"command": "dir", "cwd": "."}
    assert [(edge.source, edge.target) for edge in dag.edges] == [
        ("start", "list_files"),
        ("list_files", "read_readme"),
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


def test_llm_dag_agent_rejects_plan_spec_node_without_tool() -> None:
    provider = MockProvider([ChatResponse(content=plan_spec_json_without_tool())])
    dag_agent = LLMDAGAgent(provider)

    with pytest.raises(DAGCreationError, match="must declare one concrete tool"):
        run(dag_agent.aplan("list files", task_id="task_real"))


def test_llm_dag_agent_rejects_full_dag_node_without_tool() -> None:
    provider = MockProvider([ChatResponse(content=full_dag_json_without_tool_node())])
    dag_agent = LLMDAGAgent(provider)

    with pytest.raises(DAGCreationError, match="must declare one concrete tool"):
        run(dag_agent.aplan("Choose a strategy", task_id="task_real"))


def test_llm_dag_agent_normalizes_common_boundary_mode_aliases() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                content=dag_agent_json_with_boundary_modes(
                    ["write_only", "read_write", "read-write"]
                )
            )
        ]
    )
    dag_agent = LLMDAGAgent(provider)

    dag = run(dag_agent.aplan("Plan edits", task_id="task_real"))

    assert [node.boundary.mode for node in dag.nodes] == [
        "write_limited",
        "write_limited",
        "write_limited",
    ]


def test_harness_runtime_auto_approves_low_risk_dag_and_executes() -> None:
    provider = MockProvider([ChatResponse(content=dag_agent_json())])
    dag_agent = LLMDAGAgent(provider)
    executor = DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor())
    runtime = runtime_for(dag_agent=dag_agent, executor=executor)

    record = run(runtime.create_dag("Do a safe task", task_id="task_1"))
    result = run(runtime.execute_dag(record.task_id))

    assert record.dag.status == "completed"
    assert result.completed is True
    assert [event.event_type for event in result.traces] == [
        "dag_started",
        "node_started",
        "tool_called",
        "tool_completed",
        "node_completed",
        "dag_completed",
    ]


def test_harness_runtime_executes_layers_without_success_path_replanning() -> None:
    """Success-path replanning was removed — layers execute with original args."""
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
        dag_agent=LLMDAGAgent(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_replan"] = TaskRecord(
        task_id="task_replan",
        user_request="Use observation downstream",
        dag=prepared,
        review_level="fast",
    )

    result = run(runtime.execute_dag("task_replan"))

    assert result.completed is True
    assert result.node_results["inspect"].final_response == "echo:observed"
    assert result.node_results["answer"].final_response == "echo:old"
    assert [event.event_type for event in result.traces] == [
        "dag_started",
        "node_started",
        "tool_called",
        "tool_completed",
        "node_completed",
        "node_started",
        "tool_called",
        "tool_completed",
        "node_completed",
        "dag_completed",
    ]


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
        dag_agent=LLMDAGAgent(MockProvider([ChatResponse(content=dag_json(replacement))])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_failure_replan"] = TaskRecord(
        task_id="task_failure_replan",
        user_request="Recover from failure",
        dag=prepared,
        review_level="fast",
    )

    result = run(runtime.execute_dag("task_failure_replan"))

    assert result.completed is True
    assert result.node_results["fallback"].final_response == "echo:recovered"
    request = runtime.dag_agent.provider.requests[0]["messages"][-1]["content"]
    assert '"failed_node_id": "try_bad_tool"' in request
    assert "failed:boom" in request
    assert "dag_replanned" in [event.event_type for event in result.traces]


def test_harness_runtime_pauses_when_failed_tool_cannot_be_replanned() -> None:
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
        dag_agent=LLMDAGAgent(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_failure_needs_review"] = TaskRecord(
        task_id="task_failure_needs_review",
        user_request="Recover from failure",
        dag=prepared,
        review_level="fast",
    )

    result = run(runtime.execute_dag("task_failure_needs_review"))

    record = runtime.tasks["task_failure_needs_review"]
    assert result.completed is False
    assert record.dag.status == "paused_for_replan"
    assert record.dag.nodes[0].status == "failed"
    assert record.pending_review is not None
    assert record.pending_review.kind == "execution_error"
    assert record.pending_review.payload["failed_node_id"] == "try_bad_tool"
    assert "failed:boom" in record.pending_review.payload["error"]
    assert [event.event_type for event in result.traces][-1] == "review_requested"


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
        dag_agent=LLMDAGAgent(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_parallel_failure"] = TaskRecord(
        task_id="task_parallel_failure",
        user_request="Recover from parallel failure",
        dag=prepared,
        review_level="fast",
    )

    result = run(runtime.execute_dag("task_parallel_failure"))
    record = runtime.tasks["task_parallel_failure"]

    assert result.completed is False
    assert result.node_results["ok"].final_response == "echo:kept"
    assert record.node_results["ok"].final_response == "echo:kept"
    node_statuses = {node.id: node.status for node in record.dag.nodes}
    assert node_statuses["ok"] == "completed"
    assert node_statuses["bad_a"] == "failed"
    assert record.pending_review is not None
    assert record.dag.status == "paused_for_replan"


def test_harness_runtime_execution_error_review_requires_dag_edit_before_resume() -> None:
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
        dag_agent=LLMDAGAgent(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_failure_requires_edit"] = TaskRecord(
        task_id="task_failure_requires_edit",
        user_request="Recover from failure",
        dag=prepared,
        review_level="fast",
    )

    result = run(runtime.execute_dag("task_failure_requires_edit"))
    record = runtime.tasks["task_failure_requires_edit"]
    assert result.completed is False
    assert record.pending_review is not None
    run_count = len(record.runs)

    resumed = run(runtime.resume_dag("task_failure_requires_edit", record.pending_review.proposed_dag))

    assert resumed.status == "awaiting_change_review"
    assert resumed.pending_review is record.pending_review
    assert "without any node or edge changes" in resumed.message_markdown
    assert len(record.runs) == run_count


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
        dag_agent=LLMDAGAgent(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_dag_agent_failure_review"] = TaskRecord(
        task_id="task_dag_agent_failure_review",
        user_request="Recover from failure",
        dag=prepared,
        review_level="fast",
    )

    result = run(runtime.execute_dag("task_dag_agent_failure_review"))

    record = runtime.tasks["task_dag_agent_failure_review"]
    assert result.completed is False
    assert record.pending_review is not None
    assert record.pending_review.kind == "execution_error"
    assert "failed:boom" in record.pending_review.payload["error"]
    assert record.dag.status == "paused_for_replan"


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
        dag_agent=LLMDAGAgent(MockProvider([ChatResponse(content=dag_json(replacement))])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_stale_trace"] = TaskRecord(
        task_id="task_stale_trace",
        user_request="Recover from stale failed node traces",
        dag=prepared,
        review_level="fast",
    )

    result = run(runtime.execute_dag("task_stale_trace"))

    record = runtime.tasks["task_stale_trace"]
    assert result.completed is False
    assert record.pending_review is not None
    assert record.pending_review.kind == "execution_error"
    assert record.pending_review.payload["failed_node_id"] == "current_failure"
    assert "search_config" not in {node.id for node in record.dag.nodes}
    assert {node.id: node.status for node in record.dag.nodes}["current_failure"] == "failed"


def test_harness_runtime_patches_failed_node_and_retries() -> None:
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
        dag_agent=LLMDAGAgent(MockProvider([ChatResponse(content=dag_json(replacement))])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_patch_retry"] = TaskRecord(
        task_id="task_patch_retry",
        user_request="Patch retry",
        dag=prepared,
        review_level="fast",
    )

    result = run(runtime.execute_dag("task_patch_retry"))

    assert result.completed is True
    assert result.node_results["fragile"].final_response == "fixed-ok"
    assert runtime.tasks["task_patch_retry"].dag.nodes[0].args == {"text": "fixed"}
    assert [event.event_type for event in result.traces] == [
        "dag_started",
        "node_started",
        "tool_called",
        "tool_failed",
        "node_failed",
        "dag_failed",
        "dag_replanned",
        "node_started",
        "tool_called",
        "tool_completed",
        "node_completed",
        "dag_completed",
    ]
    records = runtime.dag_executor.trace_store.records_for_task("task_patch_retry")
    assert [record.status for record in records] == ["failed", "completed"]


def test_harness_runtime_careful_policy_pauses_for_dag_revision_review() -> None:
    initial = DAG(
        dag_id="dag_patch_review",
        task_id="task_patch_review",
        status="approved",
        nodes=[
            _tool_node("fragile", "fail_unless_fixed", {"text": "bad"}),
        ],
        edges=[],
    )
    replacement = DAG(
        dag_id="replacement",
        task_id="task_patch_review",
        nodes=[
            _tool_node("fragile", "fail_unless_fixed", {"text": "fixed"}),
        ],
        edges=[],
    )
    runtime = runtime_for(
        dag_agent=LLMDAGAgent(MockProvider([ChatResponse(content=dag_json(replacement))])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_patch_review"] = TaskRecord(
        task_id="task_patch_review",
        user_request="Patch retry",
        dag=prepared,
        review_level="careful",
    )

    result = run(runtime.execute_dag("task_patch_review"))

    record = runtime.tasks["task_patch_review"]
    assert result.completed is False
    assert record.pending_review is not None
    assert record.pending_review.kind == "dag_replan"
    assert record.dag.status == "paused_for_replan"
    assert record.pending_review.proposed_dag.nodes[0].args == {"text": "fixed"}

    resumed = run(runtime.resume_dag("task_patch_review", record.pending_review.proposed_dag))

    assert resumed.status == "completed"
    assert resumed.run_result is not None
    assert resumed.run_result.node_results["fragile"].final_response == "fixed-ok"


def test_harness_runtime_careful_policy_pauses_for_arg_injection_review() -> None:
    initial = DAG(
        dag_id="dag_arg_review",
        task_id="task_arg_review",
        status="approved",
        nodes=[
            _tool_node("inspect", "echo", {"text": "fixed"}),
            _tool_node("answer", "fail_unless_echo_fixed", {"text": "{{inspect.output}}"}),
        ],
        edges=[DAGEdge(source="inspect", target="answer")],
    )
    runtime = runtime_for(
        dag_agent=LLMDAGAgent(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_arg_review"] = TaskRecord(
        task_id="task_arg_review",
        user_request="Inject args",
        dag=prepared,
        review_level="careful",
        node_results={
            "inspect": NodeExecutionResult(
                node_id="inspect",
                final_response="echo:fixed",
                completed=True,
                stop_reason="completed",
                steps=1,
            ),
        },
    )

    result = run(runtime.execute_dag("task_arg_review"))

    record = runtime.tasks["task_arg_review"]
    assert result.completed is False
    assert record.pending_review is not None
    assert record.pending_review.kind == "arg_injection"
    assert record.dag.status == "review_required"
    assert record.pending_review.proposed_dag.nodes[1].args == {"text": "echo:fixed"}

    resumed = run(runtime.resume_dag("task_arg_review", record.pending_review.proposed_dag))

    assert resumed.status == "completed"
    assert resumed.run_result is not None
    assert resumed.run_result.node_results["answer"].final_response == "accepted:echo:fixed"


def test_harness_runtime_manual_node_execution_review_uses_review_required_status() -> None:
    initial = DAG(
        dag_id="dag_node_execution_review",
        task_id="task_node_execution_review",
        status="approved",
        nodes=[
            _tool_node("answer", "echo", {"text": "ok"}),
        ],
        edges=[],
    )
    runtime = runtime_for(
        dag_agent=LLMDAGAgent(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_node_execution_review"] = TaskRecord(
        task_id="task_node_execution_review",
        user_request="Review node execution",
        dag=prepared,
        review_level="manual",
    )

    result = run(runtime.execute_dag("task_node_execution_review"))

    record = runtime.tasks["task_node_execution_review"]
    assert result.completed is False
    assert record.pending_review is not None
    assert record.pending_review.kind == "node_execution"
    assert record.dag.status == "review_required"


def test_harness_runtime_resume_falls_back_when_summary_is_empty() -> None:
    initial = DAG(
        dag_id="dag_empty_summary",
        task_id="task_empty_summary",
        status="review_required",
        nodes=[
            _tool_node("answer", "echo", {"text": "reviewed"}),
        ],
        edges=[],
    )
    runtime = runtime_for(
        dag_agent=LLMDAGAgent(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
        agent_loop=EmptySummaryLoop(),
    )
    runtime.tasks["task_empty_summary"] = TaskRecord(
        task_id="task_empty_summary",
        user_request="Answer through DAG",
        dag=initial,
        review_level="fast",
    )

    resumed = run(runtime.resume_dag("task_empty_summary", initial))

    assert resumed.status == "completed"
    assert resumed.message_markdown
    assert "answer" in resumed.message_markdown
    assert "echo:reviewed" in resumed.message_markdown


def test_harness_runtime_resume_invalidates_modified_completed_node_and_downstream_results() -> None:
    initial = DAG(
        dag_id="dag_resume_edit",
        task_id="task_resume_edit",
        status="completed",
        nodes=[
            _tool_node("inspect", "echo", {"text": "old"}),
            _tool_node("answer", "fail_unless_echo_fixed", {"text": "{{inspect.output}}"}),
        ],
        edges=[DAGEdge(source="inspect", target="answer")],
    )
    runtime = runtime_for(
        dag_agent=LLMDAGAgent(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
    )
    prepared = runtime.prepare_dag_for_review(initial)
    prepared.status = "completed"
    for node in prepared.nodes:
        node.status = "completed"
    runtime.tasks["task_resume_edit"] = TaskRecord(
        task_id="task_resume_edit",
        user_request="Rerun edited node",
        dag=prepared,
        node_results={
            "inspect": NodeExecutionResult(
                node_id="inspect",
                final_response="echo:old",
                completed=True,
                stop_reason="completed",
                steps=1,
            ),
            "answer": NodeExecutionResult(
                node_id="answer",
                final_response="accepted:echo:old",
                completed=True,
                stop_reason="completed",
                steps=1,
            ),
        },
    )
    edited = prepared.model_copy(deep=True)
    edited.nodes[0].args = {"text": "fixed"}

    resumed = run(runtime.resume_dag("task_resume_edit", edited))

    assert resumed.status == "completed"
    assert resumed.run_result is not None
    assert resumed.run_result.node_results["inspect"].final_response == "echo:fixed"
    assert resumed.run_result.node_results["answer"].final_response == "accepted:echo:fixed"
    assert runtime.tasks["task_resume_edit"].node_results["inspect"].final_response == "echo:fixed"
    assert runtime.tasks["task_resume_edit"].node_results["answer"].final_response == "accepted:echo:fixed"


def test_harness_runtime_resume_rejects_invalid_dag_without_mutating_existing_results() -> None:
    initial = DAG(
        dag_id="dag_resume_invalid",
        task_id="task_resume_invalid",
        status="completed",
        nodes=[
            _tool_node("inspect", "echo", {"text": "old"}),
        ],
        edges=[],
    )
    runtime = runtime_for(
        dag_agent=LLMDAGAgent(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
    )
    prepared = runtime.prepare_dag_for_review(initial)
    prepared.status = "completed"
    prepared.nodes[0].status = "completed"
    runtime.tasks["task_resume_invalid"] = TaskRecord(
        task_id="task_resume_invalid",
        user_request="Reject invalid edit",
        dag=prepared,
        node_results={
            "inspect": NodeExecutionResult(
                node_id="inspect",
                final_response="echo:old",
                completed=True,
                stop_reason="completed",
                steps=1,
            ),
        },
    )
    edited = prepared.model_copy(deep=True)
    edited.nodes = []

    with pytest.raises(DAGValidationError, match="at least one node"):
        run(runtime.resume_dag("task_resume_invalid", edited))

    record = runtime.tasks["task_resume_invalid"]
    assert record.dag.nodes[0].id == "inspect"
    assert record.node_results["inspect"].final_response == "echo:old"


def test_harness_runtime_marks_node_failed_when_dag_agent_cannot_repair() -> None:
    initial = DAG(
        dag_id="dag_abort_failed_node",
        task_id="task_abort_failed_node",
        status="approved",
        nodes=[
            _tool_node("fragile", "fail_tool", {"text": "boom"}),
        ],
        edges=[],
    )
    runtime = runtime_for(
        dag_agent=LLMDAGAgent(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_abort_failed_node"] = TaskRecord(
        task_id="task_abort_failed_node",
        user_request="Abort failed node",
        dag=prepared,
    )

    result = run(runtime.execute_dag("task_abort_failed_node"))

    assert result.completed is False
    assert runtime.tasks["task_abort_failed_node"].dag.status == "paused_for_replan"
    assert runtime.tasks["task_abort_failed_node"].dag.nodes[0].status == "failed"


def test_harness_runtime_patches_completed_node_and_invalidates_downstream_results() -> None:
    initial = DAG(
        dag_id="dag_patch_completed",
        task_id="task_patch_completed",
        status="approved",
        nodes=[
            _tool_node("list_current_files", "echo", {"text": "old"}),
            _tool_node("answer", "fail_unless_echo_fixed", {"text": "{{list_current_files.output}}"}),
        ],
        edges=[DAGEdge(source="list_current_files", target="answer")],
    )
    replacement = DAG(
        dag_id="replacement",
        task_id="task_patch_completed",
        nodes=[
            _tool_node("list_current_files", "echo", {"text": "fixed"}),
            _tool_node("answer", "fail_unless_echo_fixed", {"text": "{{list_current_files.output}}"}),
        ],
        edges=[DAGEdge(source="list_current_files", target="answer")],
    )
    runtime = runtime_for(
        dag_agent=LLMDAGAgent(MockProvider([ChatResponse(content=dag_json(replacement))])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_patch_completed"] = TaskRecord(
        task_id="task_patch_completed",
        user_request="Patch completed node",
        dag=prepared,
        review_level="fast",
        node_results={
            "list_current_files": NodeExecutionResult(
                node_id="list_current_files",
                final_response="echo:old",
                completed=True,
                stop_reason="completed",
                steps=1,
            ),
        },
    )

    result = run(runtime.execute_dag("task_patch_completed"))

    assert result.completed is True
    assert result.node_results["list_current_files"].final_response == "echo:fixed"
    assert result.node_results["answer"].final_response == "accepted:echo:fixed"
    assert "dag_replanned" in [event.event_type for event in result.traces]


def test_harness_runtime_requires_review_after_risk_override() -> None:
    provider = MockProvider([ChatResponse(content=dag_agent_json(tools=["write_file"]))])
    dag_agent = LLMDAGAgent(provider)
    executor = DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor())
    runtime = runtime_for(dag_agent=dag_agent, executor=executor)

    record = run(runtime.create_dag("Modify a file", task_id="task_1"))

    assert record.dag.status == "review_required"
    assert record.dag.nodes[0].risk == "medium"
    with pytest.raises(DAGExecutionError):
        run(runtime.execute_dag(record.task_id))

    runtime.approve_dag(record.task_id)
    result = run(runtime.execute_dag(record.task_id))
    assert result.completed is True


def test_harness_runtime_pauses_for_permission_and_resumes_after_approval() -> None:
    provider = MockProvider([ChatResponse(content=dag_agent_json(tools=["run_command"]))])
    dag_agent = LLMDAGAgent(provider)
    executor = DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor())
    runtime = runtime_for(dag_agent=dag_agent, executor=executor)

    record = run(runtime.create_dag("Run a command", task_id="task_1"))
    if record.dag.status == "review_required":
        runtime.approve_dag(record.task_id)
    first_result = run(runtime.execute_dag(record.task_id))

    assert first_result.completed is False
    assert record.dag.status == "paused_for_permission"
    assert record.pending_permission_request is not None
    assert record.pending_permission_request.node_id == "inspect"
    assert record.pending_permission_request.requested_boundary.allowed_commands == ["python"]
    assert record.dag.nodes[0].status == "blocked_permission"

    permission = runtime.approve_permission(record.task_id)
    assert permission.status == "approved"
    assert record.dag.status == "approved"
    assert record.dag.nodes[0].boundary.allowed_commands == ["python"]

    second_result = run(runtime.execute_dag(record.task_id))
    assert second_result.completed is True
    assert record.dag.status == "completed"
