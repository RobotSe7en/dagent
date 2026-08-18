import asyncio
from pathlib import Path

import pytest

import dagent
from dagent.harness_runtime.artifacts import (
    ArtifactPathError,
    materialize_artifact_uploads,
)
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import Artifact


def run(coro):
    return asyncio.run(coro)


def test_materialized_artifact_files_are_workspace_relative_and_sorted(tmp_path: Path) -> None:
    manifests = materialize_artifact_uploads(
        {
            "documents": [
                dagent.ArtifactUpload(filename="z-last.txt", content=b"z"),
                dagent.ArtifactUpload(
                    filename="nested/a-first.txt",
                    content=b"first",
                    media_type="text/plain",
                ),
            ],
            "single": [
                dagent.ArtifactUpload(
                    filename="ignored-upload-name.bin",
                    content=b"one",
                    media_type="application/octet-stream",
                )
            ],
        },
        artifacts={
            "documents": Artifact(id="documents", paths=["inputs/documents/"]),
            "single": Artifact(id="single", paths=["inputs/single.bin"]),
        },
        workspace_path=tmp_path,
    )

    assert manifests == (
        dagent.ArtifactFileManifest(
            artifact_id="documents",
            files=(
                dagent.ArtifactFileRef(
                    path="inputs/documents/nested/a-first.txt",
                    name="a-first.txt",
                    size=5,
                    media_type="text/plain",
                ),
                dagent.ArtifactFileRef(
                    path="inputs/documents/z-last.txt",
                    name="z-last.txt",
                    size=1,
                ),
            ),
        ),
        dagent.ArtifactFileManifest(
            artifact_id="single",
            files=(
                dagent.ArtifactFileRef(
                    path="inputs/single.bin",
                    name="single.bin",
                    size=3,
                    media_type="application/octet-stream",
                ),
            ),
        ),
    )
    assert (tmp_path / "inputs" / "documents" / "nested" / "a-first.txt").read_bytes() == b"first"
    assert (tmp_path / "inputs" / "single.bin").read_bytes() == b"one"


def test_artifact_upload_materialization_rejects_unsafe_duplicate_and_bounded_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = {"documents": Artifact(id="documents", paths=["inputs/documents/"])}

    with pytest.raises(ArtifactPathError, match="cannot contain '..'"):
        materialize_artifact_uploads(
            {"documents": [dagent.ArtifactUpload(filename="nested/../escape.txt", content=b"x")]},
            artifacts=artifacts,
            workspace_path=tmp_path,
        )

    with pytest.raises(ArtifactPathError, match="duplicated"):
        materialize_artifact_uploads(
            {
                "documents": [
                    dagent.ArtifactUpload(filename="same.txt", content=b"first"),
                    dagent.ArtifactUpload(filename="same.txt", content=b"second"),
                ]
            },
            artifacts=artifacts,
            workspace_path=tmp_path,
        )
    assert not (tmp_path / "inputs" / "documents" / "same.txt").exists()

    with pytest.raises(ArtifactPathError, match="overlap"):
        materialize_artifact_uploads(
            {
                "documents": [
                    dagent.ArtifactUpload(filename="nested", content=b"file"),
                    dagent.ArtifactUpload(filename="nested/child.txt", content=b"child"),
                ]
            },
            artifacts=artifacts,
            workspace_path=tmp_path,
        )

    monkeypatch.setattr(
        "dagent.harness_runtime.artifacts.MAX_ARTIFACT_UPLOAD_FILES",
        1,
    )
    with pytest.raises(ArtifactPathError, match="MAX_ARTIFACT_UPLOAD_FILES"):
        materialize_artifact_uploads(
            {
                "documents": [
                    dagent.ArtifactUpload(filename="a.txt", content=b"a"),
                    dagent.ArtifactUpload(filename="b.txt", content=b"b"),
                ]
            },
            artifacts=artifacts,
            workspace_path=tmp_path,
        )

    monkeypatch.setattr(
        "dagent.harness_runtime.artifacts.MAX_ARTIFACT_UPLOAD_FILE_BYTES",
        1,
    )
    with pytest.raises(ArtifactPathError, match="MAX_ARTIFACT_UPLOAD_FILE_BYTES"):
        materialize_artifact_uploads(
            {"documents": [dagent.ArtifactUpload(filename="large.txt", content=b"xx")]},
            artifacts=artifacts,
            workspace_path=tmp_path,
        )


def test_artifact_file_expressions_select_entries_and_empty_optional_inputs(tmp_path: Path) -> None:
    @dagent.tool
    def select(path: str, name: str, size: int, media_type: str | None) -> dict[str, object]:
        return {
            "path": path,
            "name": name,
            "size": size,
            "media_type": media_type,
        }

    @dagent.tool
    def count(files: list[dict[str, object]]) -> int:
        return len(files)

    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=MockProvider([]),
        capabilities=[select, count],
        skill_roots=[],
    )

    selected_dag = dagent.Dag("select_uploaded_file")
    uploaded = selected_dag.artifact("uploaded", "inputs/uploaded/", required=False)
    selected = dagent.Node(
        "select",
        target=select,
        inputs={
            "path": uploaded.files[0].path,
            "name": uploaded.files[0].name,
            "size": uploaded.files[0].size,
            "media_type": uploaded.files[0].media_type,
        },
    )
    selected_dag.add_node(selected)
    selected_dag.output = selected.output

    selected_result = run(runner.run(
        selected_dag,
        artifact_uploads={
            "uploaded": [
                dagent.ArtifactUpload(
                    filename="source.md",
                    content=b"source",
                    media_type="text/markdown",
                )
            ]
        },
    ))

    assert selected_result.output_value == {
        "path": "inputs/uploaded/source.md",
        "name": "source.md",
        "size": 6,
        "media_type": "text/markdown",
    }
    assert selected_dag.to_dag_spec().nodes[0].payload.invocation.arguments["path"] == {
        "$expr": {
            "type": "artifact",
            "artifact_id": "uploaded",
            "field": "files",
            "path": [0, "path"],
        }
    }

    optional_dag = dagent.Dag("empty_optional_input")
    optional = optional_dag.artifact("optional", "inputs/optional/", required=False)
    counted = dagent.Node("count", target=count, inputs={"files": optional.files})
    optional_dag.add_node(counted)
    optional_dag.output = counted.output

    optional_result = run(runner.run(optional_dag))

    assert optional_result.output_value == 0
    assert optional_result.state.input_artifact_files == ()


def test_artifact_file_manifest_drives_map_and_survives_checkpoint_resume(tmp_path: Path) -> None:
    calls: list[str] = []

    @dagent.tool(risk="high")
    def approve_gate(text: str) -> str:
        return text

    @dagent.tool
    def process_file(path: str, name: str, size: int, media_type: str | None) -> dict[str, object]:
        calls.append(path)
        return {
            "path": path,
            "name": name,
            "size": size,
            "media_type": media_type,
        }

    provider = MockProvider([
        ChatResponse(tool_calls=[
            ToolCall(
                id="call_1",
                name="tool_approve_gate",
                arguments={"text": "approved"},
            )
        ]),
        ChatResponse(content="gate complete"),
    ])
    agent = dagent.ToolAgent(
        name="gate",
        profile="conversation",
        capabilities=[approve_gate],
    )
    dag = dagent.Dag("process_uploaded_files")
    uploaded = dag.artifact("uploaded", "inputs/uploaded/", required=False)
    gate = dagent.Node("gate", target=agent, inputs={"prompt": "Approve this run."})
    mapped = dagent.MapNode(
        "process",
        target=process_file,
        over=uploaded.files,
        inputs={
            "path": dagent.item.path,
            "name": dagent.item.name,
            "size": dagent.item.size,
            "media_type": dagent.item.media_type,
        },
    )
    dag.add_node(gate)
    dag.add_node(mapped)
    dag.add_edge(gate, mapped)
    dag.output = mapped.output

    first_runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        capabilities=[process_file],
        skill_roots=[],
    )
    first = run(first_runner.run(
        dag,
        review="careful",
        artifact_uploads={
            "uploaded": [
                dagent.ArtifactUpload(filename="z-last.txt", content=b"z"),
                dagent.ArtifactUpload(
                    filename="nested/a-first.txt",
                    content=b"first",
                    media_type="text/plain",
                ),
            ]
        },
    ))

    assert first.status == "awaiting_review"
    assert first.checkpoint is not None
    assert first.state.input_artifact_files[0].model_dump(mode="json") == {
        "artifact_id": "uploaded",
        "files": [
            {
                "path": "inputs/uploaded/nested/a-first.txt",
                "name": "a-first.txt",
                "size": 5,
                "media_type": "text/plain",
            },
            {
                "path": "inputs/uploaded/z-last.txt",
                "name": "z-last.txt",
                "size": 1,
                "media_type": None,
            },
        ],
    }
    workspace = Path(first.workspace_path)
    (workspace / "inputs" / "uploaded" / "not-in-manifest.txt").write_text("ignore", encoding="utf-8")
    checkpoint = dagent.RunCheckpoint.model_validate_json(first.checkpoint.model_dump_json())

    second_runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        capabilities=[process_file],
        skill_roots=[],
    )
    second_runner.add_agent(agent)
    resumed = run(second_runner.resume(first.review.approve(), checkpoint=checkpoint))

    assert resumed is not None
    assert resumed.status == "completed"
    assert resumed.output_value == [
        {
            "path": "inputs/uploaded/nested/a-first.txt",
            "name": "a-first.txt",
            "size": 5,
            "media_type": "text/plain",
        },
        {
            "path": "inputs/uploaded/z-last.txt",
            "name": "z-last.txt",
            "size": 1,
            "media_type": None,
        },
    ]
    assert calls == [
        "inputs/uploaded/nested/a-first.txt",
        "inputs/uploaded/z-last.txt",
    ]
