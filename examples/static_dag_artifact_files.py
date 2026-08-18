"""Process the current run's uploaded artifact files with a MapNode.

Run from the repository root:

    uv run python -m examples.static_dag_artifact_files
"""

from __future__ import annotations

import asyncio
from tempfile import TemporaryDirectory

import dagent
from dagent.providers import MockProvider


@dagent.tool
def inspect_file(path: str, name: str, size: int, media_type: str | None) -> dict[str, object]:
    """Receive JSON-safe metadata for one uploaded file."""

    return {
        "path": path,
        "name": name,
        "size": size,
        "media_type": media_type,
    }


async def main() -> None:
    with TemporaryDirectory(prefix="dagent-artifact-files-") as workspace:
        dag = dagent.Dag("uploaded_files")
        uploads = dag.artifact("uploads", "inputs/uploads/", required=False)
        process = dagent.MapNode(
            "process",
            target=inspect_file,
            over=uploads.files,
            inputs={
                "path": dagent.item.path,
                "name": dagent.item.name,
                "size": dagent.item.size,
                "media_type": dagent.item.media_type,
            },
        )
        dag.add_node(process)
        dag.output = process.output

        runner = dagent.Runner(
            workspace=workspace,
            runtime_directory=".runtime",
            provider=MockProvider([]),
            capabilities=[inspect_file],
            skill_roots=[],
        )
        result = await runner.run(
            dag,
            artifact_uploads={
                "uploads": [
                    dagent.ArtifactUpload(
                        filename="notes/brief.md",
                        content=b"brief",
                        media_type="text/markdown",
                    ),
                    dagent.ArtifactUpload(filename="diagram.png", content=b"png"),
                ]
            },
        )

        print(result.output_value)
        runner.close()


if __name__ == "__main__":
    asyncio.run(main())
