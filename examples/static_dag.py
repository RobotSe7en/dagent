"""Build and run a static DAG with artifacts.

Run from the repository root:

    uv run python -m examples.static_dag
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

import dagent
from dagent.providers import MockProvider


@dagent.tool
def search(q: str) -> str:
    """Return a deterministic search result."""

    return f"found:{q}"


@dagent.tool(risk="medium", supports_context=True)
def write_note(path: str, content: str, *, context, callbacks=None) -> str:
    """Write a note at a runtime-resolved artifact path."""

    run_workspace = Path(context.workspace_path).resolve()
    resolved = Path(path).resolve()
    resolved.relative_to(run_workspace)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"wrote:{path}"


async def main() -> None:
    with TemporaryDirectory(prefix="dagent-static-") as workspace:
        dag = dagent.Dag("research_report", name="Research Report", input=str)
        report = dag.artifact("report", "outputs/report.md")

        search_node = dagent.Node("search", target=search, inputs={"q": dag.input})
        write_node = dagent.Node(
            "write_report",
            target=write_note,
            inputs={"path": report.absolute_path, "content": search_node.output},
            artifact_outputs=[report],
            boundary=dagent.Boundary(
                allowed_paths=[report.absolute_path.as_expr()],
            ),
        )
        dag.add_node(search_node)
        dag.add_node(write_node)
        dag.add_edge(search_node, write_node)

        spec = dag.to_dag_spec()
        dagent.validate_dag_spec(spec)

        runner = dagent.Runner(
            workspace=workspace,
            runtime_directory=".runtime",
            provider=MockProvider([]),
        )
        result = await runner.run(
            dag,
            graph_input="dagent sdk",
        )
        output_path = Path(result.workspace_path) / "outputs" / "report.md"

        print(result.status)
        print(output_path.read_text(encoding="utf-8"))
        runner.close()


if __name__ == "__main__":
    asyncio.run(main())
