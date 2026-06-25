"""Run an AutoAgent that lets the runtime choose tool or DAG execution.

Run from the repository root:

    uv run python -m examples.auto_agent
"""

from __future__ import annotations

import asyncio

import dagent
from dagent.providers import ChatResponse, MockProvider


@dagent.tool
def search(q: str) -> str:
    """Search a tiny in-memory index."""

    return f"found:{q}"


async def main() -> None:
    provider = MockProvider(
        [
            ChatResponse(content="dag"),
            ChatResponse(content='task: research\nlookup = tool_search(q="dagent")'),
            ChatResponse(content="Report: found:dagent"),
        ]
    )
    runner = dagent.Runner(
        provider=provider,
        capabilities=[search],
    )
    agent = dagent.AutoAgent(
        capabilities=["tool.search"],
        skills=[],
    )

    result = await runner.run(
        agent,
        messages=[{"role": "user", "content": "Research dagent."}],
    )

    print(result.kind)
    print(result.status)
    print(result.output_text)
    runner.close()


if __name__ == "__main__":
    asyncio.run(main())
