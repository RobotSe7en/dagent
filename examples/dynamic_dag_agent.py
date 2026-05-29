"""Run a dynamic DagAgent against a deterministic provider.

Run from the repository root:

    uv run python -m examples.dynamic_dag_agent
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import dagent
from dagent.providers import ChatResponse, MockProvider


@dagent.tool
def search(q: str) -> str:
    """Search a tiny in-memory index."""

    return f"found:{q}"


async def main() -> None:
    provider = MockProvider(
        [
            ChatResponse(content='task: research\nlookup = search(q="dagent")'),
            ChatResponse(content="Report: found:dagent"),
        ]
    )
    runner = dagent.Runner(
        workspace=Path(__file__).resolve().parents[1],
        provider=provider,
        capabilities=[search],
    )
    agent = dagent.DagAgent(
        capabilities=["tool.search"],
    )

    result = await runner.run(agent, "Research dagent.")

    print(result.status)
    print(result.output_text)
    print([node.id for node in result.dag.nodes] if result.dag else [])
    runner.close()


if __name__ == "__main__":
    asyncio.run(main())
