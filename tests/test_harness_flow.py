import asyncio
import json

import pytest

from dagent.harness_runtime import (
    DAGCreationError,
    DAGExecutionError,
    DAGExecutor,
    HarnessRuntime,
    LLMDagCreator,
    NodeExecutionResult,
    ReplanContext,
    ReplanDecision,
    TaskRecord,
)
from dagent.providers import ChatResponse, MockProvider
from dagent.harness_runtime import AgentLoopResult
from dagent.profiles import AgentProfile
from dagent.schemas import Boundary, DAG, DAGEdge, DAGNode
from dagent.tools.boundary import BoundaryViolation
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


class StaticReplanner:
    def __init__(self, decisions: list[ReplanDecision]) -> None:
        self.decisions = list(decisions)
        self.contexts: list[ReplanContext] = []

    async def replan(self, context: ReplanContext) -> ReplanDecision:
        self.contexts.append(context)
        if self.decisions:
            return self.decisions.pop(0)
        return ReplanDecision(action="keep", reason="done")


def run(coro):
    return asyncio.run(coro)


def runtime_for(
    *,
    dag_creator: LLMDagCreator,
    executor: DAGExecutor,
    replanner=None,
    max_replans: int = 3,
    max_node_retries: int = 2,
) -> HarnessRuntime:
    return HarnessRuntime(
        agent_loop=CompletingLoop(),
        dag_creator=dag_creator,
        dag_executor=executor,
        replanner=replanner,
        conversation_profile=AgentProfile(
            name="conversation",
            role="conversation",
            layers=["soul"],
            layer_contents={"soul": "You are a conversation agent."},
        ),
        max_replans=max_replans,
        max_node_retries=max_node_retries,
    )


def dag_creator_json(*, tools: list[str] | None = None, risk: str = "low") -> str:
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
                    "title": "Inspect",
                    "goal": "Inspect the request",
                    "kind": "tool",
                    "tool": tool,
                    "args": args,
                    "agent": None,
                    "tools": [tool],
                    "skills": [],
                    "boundary": boundary,
                    "risk": risk,
                    "risk_reason": "DagCreator estimate.",
                    "expected_output": "Inspection result.",
                    "max_steps": 2,
                    "timeout_seconds": 300,
                }
            ],
            "edges": [],
        }
    )


def dag_creator_json_with_boundary_modes(modes: list[str]) -> str:
    payload = json.loads(dag_creator_json())
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
                    "goal": "List files in the current directory.",
                    "tool": "run_command",
                    "args": {"command": "dir", "cwd": "."},
                }
            ],
        }
    )


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
            "forbidden_tools": [],
            "allowed_commands": [],
            "forbidden_commands": [],
        }
    if tool == "run_command":
        return {
            "mode": "read_only",
            "allowed_paths": ["."],
            "forbidden_tools": [],
            "allowed_commands": [],
            "forbidden_commands": [],
        }
    return {
        "mode": "read_only",
        "allowed_paths": [],
        "forbidden_tools": [],
        "allowed_commands": [],
        "forbidden_commands": [],
    }


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
    registry.register(
        name="run_command",
        handler=lambda command, cwd=".", timeout_seconds=30: f"ran:{command}:{cwd}",
        action="command",
        path_args=("cwd",),
        command_args=("command",),
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
        title=node_id.replace("_", " ").title(),
        goal=f"Run {tool}.",
        kind="tool",
        tool=tool,
        args=args,
        tools=[tool],
        boundary=Boundary(mode="read_only"),
        max_steps=1,
    )


def plan_spec_json_without_tool() -> str:
    return json.dumps(
        {
            "task": "当前目录有哪些文件？",
            "nodes": [
                {
                    "id": "list_files",
                    "goal": "列出当前目录有哪些文件。",
                    "depends_on": [],
                }
            ],
        }
    )


def plan_spec_json_with_unresolved_reasoning_node() -> str:
    return json.dumps(
        {
            "task": "Decide the best implementation strategy",
            "nodes": [
                {
                    "id": "choose_strategy",
                    "goal": "Compare two implementation strategies and choose one.",
                    "depends_on": [],
                }
            ],
        }
    )


def full_dag_json_with_legacy_single_tool_node() -> str:
    payload = json.loads(dag_creator_json(tools=["run_command"]))
    payload["nodes"][0]["id"] = "list_files"
    payload["nodes"][0]["title"] = "List Files"
    payload["nodes"][0]["goal"] = "List files in the current directory."
    payload["nodes"][0].pop("tool", None)
    payload["nodes"][0].pop("args", None)
    payload["nodes"][0]["expected_output"] = "Directory listing."
    return json.dumps(payload)


def full_dag_json_without_tool_node() -> str:
    payload = json.loads(dag_creator_json())
    payload["nodes"][0].pop("tool", None)
    payload["nodes"][0].pop("args", None)
    payload["nodes"][0]["tools"] = []
    payload["nodes"][0]["kind"] = "agent"
    payload["nodes"][0]["goal"] = "Compare two implementation strategies."
    return json.dumps(payload)


def test_llm_dag_creator_parses_model_json_into_dag() -> None:
    provider = MockProvider([ChatResponse(content=dag_creator_json())])
    dag_creator = LLMDagCreator(
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

    dag = run(dag_creator.aplan("Plan something", task_id="task_real"))

    assert dag.dag_id == "dag_real"
    assert dag.task_id == "task_real"
    assert dag.nodes[0].id == "inspect"
    assert provider.requests[0]["messages"][0]["role"] == "system"
    assert "dag_creator" in provider.requests[0]["messages"][0]["content"]
    assert "read_file: Read files." in provider.requests[0]["messages"][0]["content"]
    assert "task_real" in provider.requests[0]["messages"][1]["content"]


def test_llm_dag_creator_compiles_compact_plan_spec_into_dag() -> None:
    provider = MockProvider([ChatResponse(content=plan_spec_json())])
    dag_creator = LLMDagCreator(provider)

    dag = run(dag_creator.aplan("What files are here?", task_id="task_real"))

    assert dag.task_id == "task_real"
    assert dag.nodes[0].id == "list_files"
    assert dag.nodes[0].title == "List Files"
    assert dag.nodes[0].kind == "tool"
    assert dag.nodes[0].tool == "run_command"
    assert dag.nodes[0].args == {"command": "dir", "cwd": "."}
    assert dag.nodes[0].tools == ["run_command"]
    assert dag.nodes[0].boundary.mode == "read_only"
    assert dag.nodes[0].boundary.allowed_paths == ["."]
    assert dag.nodes[0].boundary.allowed_commands == []
    assert dag.nodes[0].max_steps == 1


def test_llm_dag_creator_infers_obvious_tool_node_when_model_omits_tool() -> None:
    provider = MockProvider([ChatResponse(content=plan_spec_json_without_tool())])
    dag_creator = LLMDagCreator(provider)

    dag = run(dag_creator.aplan("当前目录有哪些文件？", task_id="task_real"))

    assert dag.nodes[0].kind == "tool"
    assert dag.nodes[0].tool == "run_command"
    assert dag.nodes[0].args == {"command": "dir", "cwd": "."}
    assert dag.nodes[0].tools == ["run_command"]


def test_llm_dag_creator_rejects_plan_spec_node_without_concrete_tool() -> None:
    provider = MockProvider([ChatResponse(content=plan_spec_json_with_unresolved_reasoning_node())])
    dag_creator = LLMDagCreator(provider)

    with pytest.raises(DAGCreationError, match="must declare one concrete tool"):
        run(dag_creator.aplan("Choose a strategy", task_id="task_real"))


def test_llm_dag_creator_normalizes_legacy_full_dag_single_tool_node() -> None:
    provider = MockProvider([ChatResponse(content=full_dag_json_with_legacy_single_tool_node())])
    dag_creator = LLMDagCreator(provider)

    dag = run(dag_creator.aplan("当前目录有哪些文件？", task_id="task_real"))

    assert dag.nodes[0].kind == "tool"
    assert dag.nodes[0].tool == "run_command"
    assert dag.nodes[0].args == {"command": "dir", "cwd": "."}
    assert dag.nodes[0].max_steps == 1


def test_llm_dag_creator_rejects_full_dag_node_without_concrete_tool() -> None:
    provider = MockProvider([ChatResponse(content=full_dag_json_without_tool_node())])
    dag_creator = LLMDagCreator(provider)

    with pytest.raises(DAGCreationError, match="must declare one concrete tool"):
        run(dag_creator.aplan("Choose a strategy", task_id="task_real"))


def test_llm_dag_creator_normalizes_common_boundary_mode_aliases() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                content=dag_creator_json_with_boundary_modes(
                    ["write_only", "read_write", "read-write"]
                )
            )
        ]
    )
    dag_creator = LLMDagCreator(provider)

    dag = run(dag_creator.aplan("Plan edits", task_id="task_real"))

    assert [node.boundary.mode for node in dag.nodes] == [
        "write_limited",
        "write_limited",
        "write_limited",
    ]


def test_harness_runtime_auto_approves_low_risk_dag_and_executes() -> None:
    provider = MockProvider([ChatResponse(content=dag_creator_json())])
    dag_creator = LLMDagCreator(provider)
    executor = DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor())
    runtime = runtime_for(dag_creator=dag_creator, executor=executor)

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


def test_harness_runtime_replans_unfinished_nodes_between_layers() -> None:
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
    replacement = DAG(
        dag_id="replacement",
        task_id="task_replan",
        nodes=[
            _tool_node("answer", "echo", {"text": "{{inspect.output}}"}),
        ],
        edges=[DAGEdge(source="inspect", target="answer")],
    )
    replanner = StaticReplanner(
        [
            ReplanDecision(
                action="replace",
                reason="Inject observed output into downstream node.",
                dag=replacement,
            )
        ]
    )
    runtime = runtime_for(
        dag_creator=LLMDagCreator(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
        replanner=replanner,
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
    assert result.node_results["answer"].final_response == "echo:echo:observed"
    stored = runtime.tasks["task_replan"].dag
    assert stored.version == 2
    assert [event.event_type for event in result.traces] == [
        "dag_started",
        "node_started",
        "tool_called",
        "tool_completed",
        "node_completed",
        "dag_replanned",
        "node_started",
        "tool_called",
        "tool_completed",
        "node_completed",
        "dag_completed",
    ]
    assert replanner.contexts[0].node_results["inspect"].final_response == "echo:observed"


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
    replanner = StaticReplanner(
        [
            ReplanDecision(
                action="replace",
                reason="Use fallback after failed tool.",
                dag=replacement,
            )
        ]
    )
    runtime = runtime_for(
        dag_creator=LLMDagCreator(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
        replanner=replanner,
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
    assert replanner.contexts[0].failed_node_id == "try_bad_tool"
    assert "failed:boom" in replanner.contexts[0].last_error
    assert "dag_replanned" in [event.event_type for event in result.traces]


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
    replanner = StaticReplanner(
        [
            ReplanDecision(
                action="patch_node",
                reason="Fix failed node args and retry.",
                node_id="fragile",
                args={"text": "fixed"},
            )
        ]
    )
    runtime = runtime_for(
        dag_creator=LLMDagCreator(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
        replanner=replanner,
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_patch_retry"] = TaskRecord(
        task_id="task_patch_retry",
        user_request="Patch retry",
        dag=prepared,
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
        "node_patched",
        "node_started",
        "tool_called",
        "tool_completed",
        "node_completed",
        "dag_completed",
    ]
    records = runtime.dag_executor.trace_store.records_for_task("task_patch_retry")
    assert [record.status for record in records] == ["failed", "completed"]


def test_harness_runtime_careful_policy_pauses_for_node_patch_review() -> None:
    initial = DAG(
        dag_id="dag_patch_review",
        task_id="task_patch_review",
        status="approved",
        nodes=[
            _tool_node("fragile", "fail_unless_fixed", {"text": "bad"}),
        ],
        edges=[],
    )
    replanner = StaticReplanner(
        [
            ReplanDecision(
                action="patch_node",
                reason="Fix failed node args and retry.",
                node_id="fragile",
                args={"text": "fixed"},
            )
        ]
    )
    runtime = runtime_for(
        dag_creator=LLMDagCreator(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
        replanner=replanner,
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
    assert record.pending_review.kind == "node_patch"
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
        dag_creator=LLMDagCreator(MockProvider([])),
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
    assert record.pending_review.proposed_dag.nodes[1].args == {"text": "echo:fixed"}

    resumed = run(runtime.resume_dag("task_arg_review", record.pending_review.proposed_dag))

    assert resumed.status == "completed"
    assert resumed.run_result is not None
    assert resumed.run_result.node_results["answer"].final_response == "accepted:echo:fixed"


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
        dag_creator=LLMDagCreator(MockProvider([])),
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


def test_harness_runtime_marks_node_failed_when_replanner_aborts() -> None:
    initial = DAG(
        dag_id="dag_abort_failed_node",
        task_id="task_abort_failed_node",
        status="approved",
        nodes=[
            _tool_node("fragile", "fail_tool", {"text": "boom"}),
        ],
        edges=[],
    )
    replanner = StaticReplanner(
        [
            ReplanDecision(
                action="abort",
                reason="Cannot repair.",
            )
        ]
    )
    runtime = runtime_for(
        dag_creator=LLMDagCreator(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
        replanner=replanner,
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_abort_failed_node"] = TaskRecord(
        task_id="task_abort_failed_node",
        user_request="Abort failed node",
        dag=prepared,
    )

    result = run(runtime.execute_dag("task_abort_failed_node"))

    assert result.completed is False
    assert runtime.tasks["task_abort_failed_node"].dag.status == "aborted"
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
    replanner = StaticReplanner(
        [
            ReplanDecision(
                action="patch_node",
                reason="Rerun completed node with corrected args.",
                node_id="list_current_files",
                args={"text": "fixed"},
            )
        ]
    )
    runtime = runtime_for(
        dag_creator=LLMDagCreator(MockProvider([])),
        executor=DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor()),
        replanner=replanner,
    )
    prepared = runtime.prepare_dag_for_review(initial)
    runtime.tasks["task_patch_completed"] = TaskRecord(
        task_id="task_patch_completed",
        user_request="Patch completed node",
        dag=prepared,
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
    assert "node_patched" in [event.event_type for event in result.traces]


def test_harness_runtime_requires_review_after_risk_override() -> None:
    provider = MockProvider([ChatResponse(content=dag_creator_json(tools=["write_file"]))])
    dag_creator = LLMDagCreator(provider)
    executor = DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor())
    runtime = runtime_for(dag_creator=dag_creator, executor=executor)

    record = run(runtime.create_dag("Modify a file", task_id="task_1"))

    assert record.dag.status == "review_required"
    assert record.dag.nodes[0].risk == "medium"
    with pytest.raises(DAGExecutionError):
        run(runtime.execute_dag(record.task_id))

    runtime.approve_dag(record.task_id)
    result = run(runtime.execute_dag(record.task_id))
    assert result.completed is True


def test_harness_runtime_pauses_for_permission_and_resumes_after_approval() -> None:
    provider = MockProvider([ChatResponse(content=dag_creator_json(tools=["run_command"]))])
    dag_creator = LLMDagCreator(provider)
    executor = DAGExecutor(agent_loop=CompletingLoop(), tool_executor=make_tool_executor())
    runtime = runtime_for(dag_creator=dag_creator, executor=executor)

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
