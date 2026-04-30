import asyncio
import json

import pytest

from dagent.harness_runtime import ControlPlane, DAGExecutionError, DAGExecutor, LLMDagCreator
from dagent.providers import ChatResponse, MockProvider
from dagent.harness_runtime import AgentLoopResult
from dagent.schemas import Boundary
from dagent.tools.boundary import BoundaryViolation
from dagent.tools.registry import Tool


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


def run(coro):
    return asyncio.run(coro)


def dag_creator_json(*, tools: list[str] | None = None, risk: str = "low") -> str:
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


def full_dag_json_with_legacy_single_tool_node() -> str:
    payload = json.loads(dag_creator_json(tools=["run_command"]))
    payload["nodes"][0]["id"] = "list_files"
    payload["nodes"][0]["title"] = "List Files"
    payload["nodes"][0]["goal"] = "List files in the current directory."
    payload["nodes"][0]["expected_output"] = "Directory listing."
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


def test_llm_dag_creator_normalizes_legacy_full_dag_single_tool_node() -> None:
    provider = MockProvider([ChatResponse(content=full_dag_json_with_legacy_single_tool_node())])
    dag_creator = LLMDagCreator(provider)

    dag = run(dag_creator.aplan("当前目录有哪些文件？", task_id="task_real"))

    assert dag.nodes[0].kind == "tool"
    assert dag.nodes[0].tool == "run_command"
    assert dag.nodes[0].args == {"command": "dir", "cwd": "."}
    assert dag.nodes[0].max_steps == 1


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


def test_control_plane_auto_approves_low_risk_dag_and_executes() -> None:
    provider = MockProvider([ChatResponse(content=dag_creator_json())])
    dag_creator = LLMDagCreator(provider)
    executor = DAGExecutor(agent_loop=CompletingLoop())
    control_plane = ControlPlane(dag_creator=dag_creator, executor=executor)

    record = run(control_plane.create_task("Do a safe task", task_id="task_1"))
    result = run(control_plane.execute_task(record.task_id))

    assert record.dag.status == "completed"
    assert result.completed is True
    assert [event.event_type for event in result.traces] == [
        "dag_started",
        "node_started",
        "node_completed",
        "dag_completed",
    ]


def test_control_plane_requires_review_after_risk_override() -> None:
    provider = MockProvider([ChatResponse(content=dag_creator_json(tools=["write_file"]))])
    dag_creator = LLMDagCreator(provider)
    executor = DAGExecutor(agent_loop=CompletingLoop())
    control_plane = ControlPlane(dag_creator=dag_creator, executor=executor)

    record = run(control_plane.create_task("Modify a file", task_id="task_1"))

    assert record.dag.status == "review_required"
    assert record.dag.nodes[0].risk == "medium"
    with pytest.raises(DAGExecutionError):
        run(control_plane.execute_task(record.task_id))

    control_plane.approve_dag(record.task_id)
    result = run(control_plane.execute_task(record.task_id))
    assert result.completed is True


def test_control_plane_pauses_for_permission_and_resumes_after_approval() -> None:
    provider = MockProvider([ChatResponse(content=dag_creator_json())])
    dag_creator = LLMDagCreator(provider)
    loop = PermissionThenCompletingLoop()
    executor = DAGExecutor(agent_loop=loop)
    control_plane = ControlPlane(dag_creator=dag_creator, executor=executor)

    record = run(control_plane.create_task("Run a command", task_id="task_1"))
    first_result = run(control_plane.execute_task(record.task_id))

    assert first_result.completed is False
    assert record.dag.status == "paused_for_permission"
    assert record.pending_permission_request is not None
    assert record.pending_permission_request.node_id == "inspect"
    assert record.pending_permission_request.requested_boundary.allowed_commands == ["python"]
    assert record.dag.nodes[0].status == "blocked_permission"

    permission = control_plane.approve_permission(record.task_id)
    assert permission.status == "approved"
    assert record.dag.status == "approved"
    assert record.dag.nodes[0].boundary.allowed_commands == ["python"]

    second_result = run(control_plane.execute_task(record.task_id))
    assert second_result.completed is True
    assert record.dag.status == "completed"
    assert loop.calls == 2
