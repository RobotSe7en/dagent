import asyncio
from pathlib import Path
from typing import Any

import pytest

import dagent.schemas.results as results_schema
from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.harness_runtime import CapabilityExecutor, DAGAgent, DAGAgentLoop, DAGExecutor
from dagent.harness_runtime.artifacts import (
    ArtifactPathError,
    init_artifact_states,
    update_node_output_artifacts,
    validate_artifact_paths,
)
from dagent.harness_runtime.dag_builder import (
    DAGValidationError,
    compile_dag_spec,
    validate_dag_spec,
)
from dagent.schemas import CapabilityDefinition, CapabilityPolicy
from dagent.schemas import (
    Artifact,
    ArtifactState,
    Boundary,
    CapabilityInvocation,
    DAGEdge,
    DAGNode,
    DAGSpec,
    LoopOutcome,
)
from dagent.schemas.results import DAGStepResult
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider
from dagent.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def test_artifact_supports_multiple_relative_paths() -> None:
    artifact = Artifact(
        id="requirement_package",
        paths=["requirements/spec.md", "requirements/assets/"],
        description="Requirement package.",
    )

    assert artifact.id == "requirement_package"
    assert artifact.paths == ["requirements/spec.md", "requirements/assets/"]
    assert artifact.required is True
    assert artifact.metadata == {}


def test_dag_node_binds_artifact_ids_as_inputs_and_outputs() -> None:
    node = _node(
        "write_requirement",
        inputs=["raw_requirement"],
        outputs=["requirement_package"],
    )

    assert node.title == "Write requirement"
    assert node.goal is None
    assert node.instructions is None
    assert node.inputs == ["raw_requirement"]
    assert node.outputs == ["requirement_package"]


def test_dag_node_model_validate_keeps_goal_and_instructions_optional() -> None:
    node = DAGNode.model_validate({
        "id": "read",
        "invocation": {
            "capability_id": "tool.echo",
            "kind": "tool",
            "arguments": {"text": "ok"},
        },
    })

    assert node.goal is None
    assert node.instructions is None
    dumped = node.model_dump(mode="json")
    assert dumped["goal"] is None
    assert dumped["instructions"] is None


def test_dag_node_supports_agent_goal_and_instructions() -> None:
    node = _node(
        "write_requirement",
        goal="Write a complete requirement specification.",
        instructions="Use clear acceptance criteria.",
    )

    assert node.goal == "Write a complete requirement specification."
    assert node.instructions == "Use clear acceptance criteria."


def test_dag_run_result_alias_is_removed_from_results_schema() -> None:
    assert not hasattr(results_schema, "DAGRunResult")


def test_validate_dag_spec_rejects_unknown_node_artifact() -> None:
    spec = DAGSpec(
        id="requirements",
        name="Requirements",
        artifacts={
            "raw_requirement": Artifact(id="raw_requirement", paths=["inputs/raw.md"]),
        },
        nodes=[
            _node(
                "write_requirement",
                inputs=["raw_requirement"],
                outputs=["missing_output"],
            )
        ],
    )

    with pytest.raises(DAGValidationError, match="missing_output"):
        validate_dag_spec(spec)


@pytest.mark.parametrize("bad_path", ["C:/outside/file.md", "../outside.md", "safe/../../outside.md"])
def test_validate_artifact_paths_rejects_absolute_or_escaping_paths(bad_path: str) -> None:
    with pytest.raises(ArtifactPathError):
        validate_artifact_paths([bad_path])


@pytest.mark.parametrize("bad_path", ["C:outside/file.md", "D:/outside/file.md", "\\\\server\\share\\file.md"])
def test_validate_artifact_paths_rejects_windows_absolute_or_drive_paths(bad_path: str) -> None:
    with pytest.raises(ArtifactPathError):
        validate_artifact_paths([bad_path])


def test_resolve_artifact_paths_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    link = workspace / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is not available in this environment.")

    with pytest.raises(ArtifactPathError):
        update_node_output_artifacts(
            _node("write", outputs=["escaped"]),
            artifacts={"escaped": Artifact(id="escaped", paths=["linked/file.txt"])},
            states=init_artifact_states({"escaped": Artifact(id="escaped", paths=["linked/file.txt"])}),
            workspace_path=workspace,
        )


def test_compile_dag_spec_preserves_artifacts_on_nodes() -> None:
    spec = DAGSpec(
        id="requirements",
        name="Requirements",
        artifacts={
            "raw_requirement": Artifact(id="raw_requirement", paths=["inputs/raw.md"]),
            "requirement_package": Artifact(
                id="requirement_package",
                paths=["requirements/spec.md", "requirements/assets/"],
            ),
        },
        nodes=[
            _node(
                "write_requirement",
                inputs=["raw_requirement"],
                outputs=["requirement_package"],
            )
        ],
    )

    dag = compile_dag_spec(spec, task_id="task_1")

    assert dag.task_id == "task_1"
    assert dag.nodes[0].inputs == ["raw_requirement"]
    assert dag.nodes[0].outputs == ["requirement_package"]


def test_compile_dag_spec_preserves_node_goal_and_instructions() -> None:
    spec = DAGSpec(
        id="requirements",
        name="Requirements",
        artifacts={},
        nodes=[
            _node(
                "write_requirement",
                goal="Write the requirement spec.",
                instructions="Use numbered acceptance criteria.",
            )
        ],
    )

    dag = compile_dag_spec(spec, task_id="task_1")

    assert dag.nodes[0].goal == "Write the requirement spec."
    assert dag.nodes[0].instructions == "Use numbered acceptance criteria."


def test_compile_dag_spec_copies_capability_policy_risk() -> None:
    spec = DAGSpec(
        id="write_note",
        name="Write note",
        artifacts={},
        nodes=[
            _node(
                "write",
                tool="write_file",
                args={"path": "notes.md", "content": "hi"},
                boundary=Boundary(mode="write_limited", allowed_paths=["notes.md"]),
            )
        ],
    )

    dag = compile_dag_spec(
        spec,
        task_id="task_1",
        capabilities=[
            CapabilityDefinition(
                id="tool.write_file",
                name="write_file",
                kind="tool",
                policy=CapabilityPolicy(risk="medium"),
            )
        ],
    )

    assert dag.nodes[0].invocation.risk == "medium"


def test_artifact_states_mark_created_and_missing_outputs(tmp_path: Path) -> None:
    artifacts = {
        "created_doc": Artifact(id="created_doc", paths=["outputs/doc.md"]),
        "missing_package": Artifact(id="missing_package", paths=["outputs/missing/"]),
    }
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "doc.md").write_text("hello", encoding="utf-8")
    states = init_artifact_states(artifacts)
    node = _node("write_outputs", outputs=["created_doc", "missing_package"])

    update_node_output_artifacts(
        node,
        artifacts=artifacts,
        states=states,
        workspace_path=tmp_path,
    )

    assert states["created_doc"] == ArtifactState(
        id="created_doc",
        paths=["outputs/doc.md"],
        status="created",
        producer_node_id="write_outputs",
    )
    assert states["missing_package"].status == "missing"
    assert states["missing_package"].producer_node_id == "write_outputs"


def test_executor_updates_artifact_states_after_node_outputs(tmp_path: Path) -> None:
    executor = DAGExecutor(
        capability_executor=_write_capability_executor(tmp_path),
        workspace_path=tmp_path,
        artifacts={
            "note": Artifact(id="note", paths=["notes/output.txt"]),
        },
    )
    dag = compile_dag_spec(
        DAGSpec(
            id="write_note",
            name="Write note",
            artifacts={"note": Artifact(id="note", paths=["notes/output.txt"])},
            nodes=[
                _node(
                    "write",
                    tool="write_note",
                    args={"path": "notes/output.txt", "content": "hi"},
                    boundary=Boundary(mode="write_limited", allowed_paths=["notes/output.txt"]),
                    outputs=["note"],
                )
            ],
        ),
        task_id="task_1",
    )

    result = run(executor.execute_next_ready_layer(dag))

    assert isinstance(result, DAGStepResult)
    assert result.artifact_states["note"].status == "created"
    assert (tmp_path / "notes" / "output.txt").read_text(encoding="utf-8") == "hi"


def test_concurrent_executors_keep_workspace_context_isolated(tmp_path: Path) -> None:
    capability_executor = _write_capability_executor(tmp_path)
    workspace_a = tmp_path / "run_a"
    workspace_b = tmp_path / "run_b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    release = asyncio.Event()
    executor_a = _DelayedDAGExecutor(
        release,
        capability_executor=capability_executor,
        workspace_path=workspace_a,
        artifacts={"note": Artifact(id="note", paths=["notes/output.txt"])},
    )
    executor_b = _DelayedDAGExecutor(
        release,
        capability_executor=capability_executor,
        workspace_path=workspace_b,
        artifacts={"note": Artifact(id="note", paths=["notes/output.txt"])},
    )
    dag_a = _write_note_dag("task_a", "from-a")
    dag_b = _write_note_dag("task_b", "from-b")

    async def execute_both() -> None:
        task_a = asyncio.create_task(executor_a.execute_next_ready_layer(dag_a))
        task_b = asyncio.create_task(executor_b.execute_next_ready_layer(dag_b))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(task_a, task_b)

    run(execute_both())

    assert (workspace_a / "notes" / "output.txt").read_text(encoding="utf-8") == "from-a"
    assert (workspace_b / "notes" / "output.txt").read_text(encoding="utf-8") == "from-b"


def test_dag_agent_runs_dag_spec_as_dag_lifecycle_owner(tmp_path: Path) -> None:
    capability_executor = _write_capability_executor(tmp_path)
    agent = _dag_agent_for_executor(capability_executor)
    spec = DAGSpec(
        id="write_note",
        name="Write note",
        artifacts={
            "note": Artifact(id="note", paths=["notes/output.txt"]),
        },
        nodes=[
            _node(
                "write",
                tool="write_note",
                args={"path": "notes/output.txt", "content": "hi"},
                boundary=Boundary(mode="write_limited", allowed_paths=["notes/output.txt"]),
                outputs=["note"],
            )
        ],
    )

    outcome = run(agent.run_spec(spec, workspace_root=tmp_path / "runs"))

    assert isinstance(outcome, LoopOutcome)
    assert outcome.status == "completed"
    assert outcome.task_id is not None
    assert outcome.spec_id == "write_note"
    assert outcome.workspace_path is not None
    workspace_path = Path(outcome.workspace_path)
    assert (workspace_path / "notes" / "output.txt").read_text(encoding="utf-8") == "hi"
    assert outcome.dag is not None
    assert outcome.dag.status == "completed"
    assert outcome.artifact_states["note"].status == "created"
    assert outcome.dag_run is not None
    assert outcome.dag_run.execution_records[0].node_id == "write"


def test_dag_agent_run_spec_respects_enabled_toolsets(tmp_path: Path) -> None:
    capability_executor = _write_capability_executor(tmp_path)
    agent = _dag_agent_for_executor(
        capability_executor,
        enabled_capability_ids=(),
    )
    spec = DAGSpec(
        id="write_note",
        name="Write note",
        nodes=[
            _node(
                "write",
                tool="write_note",
                args={"path": "notes/output.txt", "content": "hi"},
            )
        ],
    )

    with pytest.raises(DAGValidationError, match="Unknown capability"):
        run(agent.run_spec(spec, workspace_root=tmp_path / "runs"))


def test_dag_agent_run_spec_preserves_partial_state_on_failure(tmp_path: Path) -> None:
    capability_executor = _write_and_fail_capability_executor(tmp_path)
    agent = _dag_agent_for_executor(capability_executor)
    spec = DAGSpec(
        id="partial_failure",
        name="Partial failure",
        artifacts={"note": Artifact(id="note", paths=["notes/output.txt"], required=False)},
        nodes=[
            _node(
                "write",
                tool="write_note",
                args={"path": "notes/output.txt", "content": "hi"},
                boundary=Boundary(mode="write_limited", allowed_paths=["notes/output.txt"]),
                outputs=["note"],
            ),
            _node(
                "fail",
                tool="fail_tool",
                args={"text": "boom"},
            ),
        ],
        edges=[DAGEdge(source="write", target="fail")],
    )

    outcome = run(agent.run_spec(spec, workspace_root=tmp_path / "runs"))

    assert outcome.status == "failed"
    assert outcome.dag_run is not None
    assert "write" in outcome.dag_run.node_results
    assert outcome.dag_run.node_results["write"].completed is True
    assert any(record.node_id == "fail" and record.status == "failed" for record in outcome.dag_run.execution_records)
    assert outcome.dag is not None
    statuses = {node.id: node.status for node in outcome.dag.nodes}
    assert statuses["write"] == "completed"
    assert statuses["fail"] == "failed"


def _node(
    node_id: str,
    *,
    tool: str = "echo",
    args: dict | None = None,
    boundary: Boundary | None = None,
    goal: str | None = None,
    instructions: str | None = None,
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
) -> DAGNode:
    return DAGNode(
        id=node_id,
        title=node_id.replace("_", " ").capitalize(),
        goal=goal,
        instructions=instructions,
        invocation=CapabilityInvocation(
            capability_id=f"tool.{tool}",
            kind="tool",
            arguments=args or {"text": node_id},
            boundary=boundary or Boundary(),
        ),
        inputs=inputs or [],
        outputs=outputs or [],
    )


def _write_capability_executor(workspace_root: Path) -> CapabilityExecutor:
    registry = ToolRegistry()
    registry.register(
        name="write_note",
        handler=_write_note,
        action="write",
        path_args=("path",),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    )
    capability_catalog = CapabilityCatalog(workspace_root=workspace_root)
    ToolCapabilityProvider(registry).register_into(capability_catalog)
    return CapabilityExecutor(capability_catalog)


def _write_note_dag(task_id: str, content: str):
    return compile_dag_spec(
        DAGSpec(
            id=task_id,
            name=task_id,
            artifacts={"note": Artifact(id="note", paths=["notes/output.txt"])},
            nodes=[
                _node(
                    "write",
                    tool="write_note",
                    args={"path": "notes/output.txt", "content": content},
                    boundary=Boundary(mode="write_limited", allowed_paths=["notes/output.txt"]),
                    outputs=["note"],
                )
            ],
        ),
        task_id=task_id,
    )


def _dag_agent_for_executor(
    capability_executor: CapabilityExecutor,
    *,
    enabled_capability_ids: tuple[str, ...] | None = None,
) -> DAGAgent:
    capability_ids = tuple(sorted(capability_executor.catalog.ids())) if enabled_capability_ids is None else enabled_capability_ids
    tool_adapter = CapabilityToolAdapter(
        capability_executor.catalog,
        toolsets=[CapabilityToolset("builtin", capability_ids)],
    )
    return DAGAgent(
        loop=DAGAgentLoop(
            provider=MockProvider([ChatResponse(content="unused")]),
            dag_executor=DAGExecutor(capability_executor=capability_executor),
            tool_adapter=tool_adapter,
        ),
        profile=AgentProfile(
            name="dag_agent",
            role="dag_agent",
            layers=["soul"],
            layer_contents={"soul": "You are a DAG agent."},
        ),
    )


def _write_and_fail_capability_executor(workspace_root: Path) -> CapabilityExecutor:
    registry = ToolRegistry()
    registry.register(
        name="write_note",
        handler=_write_note,
        action="write",
        path_args=("path",),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
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
    capability_catalog = CapabilityCatalog(workspace_root=workspace_root)
    ToolCapabilityProvider(registry).register_into(capability_catalog)
    return CapabilityExecutor(capability_catalog)


class _DelayedDAGExecutor(DAGExecutor):
    def __init__(self, release: asyncio.Event, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._release = release

    async def execute_node(self, node, dag, completed_results):
        await self._release.wait()
        return await super().execute_node(node, dag, completed_results)


def _write_note(path: str | Path, content: str) -> str:
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"wrote:{resolved}:{content}"
