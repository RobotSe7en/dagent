import asyncio
from pathlib import Path

import pytest

from dagent.capabilities import CapabilityCatalog
from dagent.capabilities.providers import ToolCapabilityProvider
from dagent.harness_runtime import CapabilityExecutor, DAGExecutor
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
from dagent.schemas import (
    Artifact,
    ArtifactState,
    Boundary,
    CapabilityInvocation,
    DAGNode,
    DAGSpec,
)
from dagent.schemas.results import DAGStepResult
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
    assert node.inputs == ["raw_requirement"]
    assert node.outputs == ["requirement_package"]


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


def _node(
    node_id: str,
    *,
    tool: str = "echo",
    args: dict | None = None,
    boundary: Boundary | None = None,
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
) -> DAGNode:
    return DAGNode(
        id=node_id,
        title=node_id.replace("_", " ").capitalize(),
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


def _write_note(path: str | Path, content: str) -> str:
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"wrote:{resolved}:{content}"
