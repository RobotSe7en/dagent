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
    """Write a note inside the run workspace."""

    resolved = Path(context.workspace_path) / path
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"wrote:{path}"


async def main() -> None:
    with TemporaryDirectory(prefix="dagent-static-") as workspace:
        dag = dagent.Dag("research_report", name="Research Report")
        report = dag.artifact("report", "outputs/report.md")

        search_node = dag.capability_node("search", search, q="dagent sdk")
        dag.capability_node(
            "write_report",
            write_note,
            path="outputs/report.md",
            content=search_node.output,
            outputs=[report],
        ).after(search_node)

        spec = dag.to_dag_spec()
        dagent.validate_dag_spec(spec)

        runner = dagent.Runner(workspace=workspace, provider=MockProvider([]))
        result = await runner.run(dag, workspace_root=Path(workspace) / "runs")
        output_path = Path(result.workspace_path) / "outputs" / "report.md"

        print(result.status)
        print(output_path.read_text(encoding="utf-8"))
        runner.close()


if __name__ == "__main__":
    asyncio.run(main())
