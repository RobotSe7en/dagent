"""Run a dynamic DagAgent against a deterministic provider.

Run from the repository root:

    uv run python -m examples.dynamic_dag_agent
"""

from __future__ import annotations

import asyncio
import json

import dagent
from dagent.providers import ChatResponse, MockProvider


@dagent.tool
def search(q: str) -> str:
    """Search a tiny in-memory index."""

    return f"found:{q}"


async def main() -> None:
    provider = MockProvider(
        [
            ChatResponse(content=json.dumps({
                "action": "propose_plan",
                "plan": {
                    "name": "research",
                    "description": "Search for dagent.",
                    "artifacts": [],
                    "nodes": [{
                        "type": "capability",
                        "id": "lookup",
                        "title": "Search",
                        "inputs": [],
                        "outputs": [],
                        "capability_id": "tool.search",
                        "arguments": [{
                            "name": "q",
                            "value": {"type": "literal", "value": "dagent"},
                        }],
                    }],
                    "edges": [],
                    "output": None,
                },
                "answer": None,
                "rerun_nodes": [],
            })),
            ChatResponse(content=json.dumps({
                "action": "final_answer",
                "plan": None,
                "answer": "Report: found:dagent",
                "rerun_nodes": [],
            })),
        ]
    )
    runner = dagent.Runner(
        workspace="agent-workspace",
        runtime_directory=".runtime",
        provider=provider,
        capabilities=[search],
    )
    agent = dagent.DagAgent(
        capabilities=["tool.search"],
    )

    result = await runner.run(
        agent,
        input="Research dagent.",
    )

    print(result.status)
    print(result.output_text)
    print([node.id for node in result.dag.nodes] if result.dag else [])
    runner.close()


if __name__ == "__main__":
    asyncio.run(main())
