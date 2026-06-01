import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import dagent.schemas.results as results_schema
from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.harness_runtime import CapabilityExecutor, DAGAgentLoop, DAGExecutor
from dagent.providers import ChatResponse, MockProvider
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
    RunTrace,
    RunTraceNode,
)
from dagent.capabilities.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def dag_node_trace(trace: RunTrace, node_id: str) -> RunTraceNode:
    for child in trace.root.children:
        if child.kind == "dag_node" and child.ref.get("node_id") == node_id:
            return child
    raise AssertionError(f"Missing dag_node trace for {node_id}")


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
    assert node.inputs == ["raw_requirement"]
    assert node.outputs == ["requirement_package"]


def test_dag_node_model_validate_uses_payload_only_shape() -> None:
    node = DAGNode.model_validate({
        "id": "read",
        "payload": {
            "type": "capability",
            "invocation": {
                "capability_id": "tool.echo",
                "kind": "tool",
                "arguments": {"text": "ok"},
            },
        },
    })

    dumped = node.model_dump(mode="json")
    assert dumped["payload"]["type"] == "capability"
    assert dumped["payload"]["invocation"]["capability_id"] == "tool.echo"
    assert "goal" not in dumped
    assert "instructions" not in dumped
    assert "invocation" not in dumped
    assert "node_type" not in dumped


def test_dag_node_rejects_legacy_invocation_shape() -> None:
    with pytest.raises(ValidationError):
        DAGNode.model_validate({
            "id": "read",
            "invocation": {
                "capability_id": "tool.echo",
                "kind": "tool",
                "arguments": {"text": "ok"},
            },
        })


def test_dag_node_rejects_goal_and_instructions_on_node_shell() -> None:
    with pytest.raises(ValidationError):
        DAGNode.model_validate({
            "id": "read",
            "goal": "Do work.",
            "instructions": "Be concise.",
            "payload": {
                "type": "capability",
                "invocation": {
                    "capability_id": "tool.echo",
                    "kind": "tool",
                    "arguments": {"text": "ok"},
                },
            },
        })


def test_dag_node_payload_discriminator_is_required_in_json_schema() -> None:
    schema = DAGNode.model_json_schema()

    capability_schema = schema["$defs"]["CapabilityNodePayload"]
    start_schema = schema["$defs"]["StartNodePayload"]

    assert "type" in capability_schema["required"]
    assert "type" in start_schema["required"]


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


def test_validate_dag_spec_rejects_duplicate_artifact_producers() -> None:
    spec = DAGSpec(
        id="requirements",
        name="Requirements",
        artifacts={
            "report": Artifact(id="report", paths=["outputs/report.md"]),
        },
        nodes=[
            _node("draft_report", outputs=["report"]),
            _node("revise_report", outputs=["report"]),
        ],
        edges=[DAGEdge(source="draft_report", target="revise_report")],
    )

    with pytest.raises(DAGValidationError, match="Artifact 'report' is produced by multiple nodes"):
        validate_dag_spec(spec)


def test_validate_dag_spec_requires_consumer_to_depend_on_artifact_producer() -> None:
    spec = DAGSpec(
        id="requirements",
        name="Requirements",
        artifacts={
            "report": Artifact(id="report", paths=["outputs/report.md"]),
        },
        nodes=[
            _node("write_report", outputs=["report"]),
            _node("review_report", inputs=["report"]),
        ],
        edges=[DAGEdge(source="review_report", target="write_report")],
    )

    with pytest.raises(DAGValidationError, match="must depend on producer node 'write_report'"):
        validate_dag_spec(spec)


def test_validate_dag_spec_allows_external_input_artifacts_without_producer() -> None:
    spec = DAGSpec(
        id="requirements",
        name="Requirements",
        artifacts={
            "uploaded_spec": Artifact(id="uploaded_spec", paths=["uploads/spec.md"]),
        },
        nodes=[
            _node("analyze_upload", inputs=["uploaded_spec"]),
        ],
    )

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


def test_compile_dag_spec_preserves_agent_prompt_argument() -> None:
    spec = DAGSpec(
        id="requirements",
        name="Requirements",
        artifacts={},
        nodes=[
            _node(
                "write_requirement",
                tool="agent.helper",
                kind="agent",
                args={"prompt": "Write the requirement spec. Use numbered acceptance criteria."},
            )
        ],
    )

    dag = compile_dag_spec(spec, task_id="task_1")

    assert dag.nodes[0].payload.invocation.arguments == {
        "prompt": "Write the requirement spec. Use numbered acceptance criteria."
    }


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

    assert dag.nodes[0].payload.invocation.risk == "medium"


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

    assert isinstance(result, RunTrace)
    assert result.artifacts["note"].status == "created"
    assert (tmp_path / "notes" / "output.txt").read_text(encoding="utf-8") == "hi"


def _expr(payload: dict) -> dict:
    return {"$expr": payload}


def test_executor_resolves_artifact_exprs_in_arguments_and_boundary(tmp_path: Path) -> None:
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
                    args={
                        "path": _expr({"type": "artifact", "artifact_id": "note", "field": "path"}),
                        "content": "hi",
                    },
                    boundary=Boundary(
                        mode="write_limited",
                        allowed_paths=[_expr({"type": "artifact", "artifact_id": "note", "field": "path"})],
                    ),
                    outputs=["note"],
                )
            ],
        ),
        task_id="task_1",
    )

    result = run(executor.execute_next_ready_layer(dag))

    invocation = dag_node_trace(result, "write").children[0].capability_execution.invocation
    assert invocation.arguments["path"] == "notes/output.txt"
    assert invocation.boundary.allowed_paths == ["notes/output.txt"]
    assert result.artifacts["note"].status == "created"
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


def test_dag_agent_loop_runs_static_dag_spec_as_dag_lifecycle_owner(tmp_path: Path) -> None:
    capability_executor = _write_capability_executor(tmp_path)
    loop = _dag_agent_loop_for_executor(capability_executor)
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

    outcome = run(loop.run_static(spec, workspace_root=tmp_path / "runs"))

    assert isinstance(outcome, LoopOutcome)
    assert outcome.status == "completed"
    assert outcome.task_id is not None
    assert outcome.spec_id == "write_note"
    assert outcome.workspace_path is not None
    workspace_path = Path(outcome.workspace_path)
    assert (workspace_path / "notes" / "output.txt").read_text(encoding="utf-8") == "hi"
    assert outcome.dag is not None
    assert outcome.dag.status == "completed"
    assert outcome.trace is not None
    assert outcome.trace.artifacts["note"].status == "created"
    assert dag_node_trace(outcome.trace, "write").children[0].capability_execution.result.content.startswith("wrote:")


def test_dag_agent_loop_run_static_respects_enabled_toolsets(tmp_path: Path) -> None:
    capability_executor = _write_capability_executor(tmp_path)
    loop = _dag_agent_loop_for_executor(
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
        run(loop.run_static(spec, workspace_root=tmp_path / "runs"))


def test_dag_agent_loop_run_static_preserves_partial_state_on_failure(tmp_path: Path) -> None:
    capability_executor = _write_and_fail_capability_executor(tmp_path)
    loop = _dag_agent_loop_for_executor(capability_executor)
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

    outcome = run(loop.run_static(spec, workspace_root=tmp_path / "runs"))

    assert outcome.status == "failed"
    assert outcome.trace is not None
    assert dag_node_trace(outcome.trace, "write").status == "completed"
    assert dag_node_trace(outcome.trace, "fail").status == "failed"
    assert outcome.dag is not None
    statuses = {node.id: node.status for node in outcome.dag.nodes}
    assert statuses["write"] == "completed"
    assert statuses["fail"] == "failed"


def _node(
    node_id: str,
    *,
    tool: str = "echo",
    kind: str = "tool",
    args: dict | None = None,
    boundary: Boundary | None = None,
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
) -> DAGNode:
    return DAGNode(
        id=node_id,
        title=node_id.replace("_", " ").capitalize(),
        payload=dict(
            type="capability",
            invocation=CapabilityInvocation(
                capability_id=tool if "." in tool else f"tool.{tool}",
                kind=kind,
                arguments=args or {"text": node_id},
                boundary=boundary or Boundary(),
            ),
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


def _dag_agent_loop_for_executor(
    capability_executor: CapabilityExecutor,
    *,
    enabled_capability_ids: tuple[str, ...] | None = None,
) -> DAGAgentLoop:
    capability_ids = tuple(sorted(capability_executor.catalog.ids())) if enabled_capability_ids is None else enabled_capability_ids
    tool_adapter = CapabilityToolAdapter(
        capability_executor.catalog,
        toolsets=[CapabilityToolset("builtin", capability_ids)],
    )
    return DAGAgentLoop(
        provider=MockProvider([ChatResponse(content="unused")]),
        dag_executor=DAGExecutor(capability_executor=capability_executor),
        tool_adapter=tool_adapter,
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

    async def execute_node(self, node, dag, **kwargs):
        await self._release.wait()
        return await super().execute_node(node, dag, **kwargs)


def _write_note(path: str | Path, content: str) -> str:
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"wrote:{resolved}:{content}"
