"""Run the optional restricted SDK-builder planner frontend offline.

Run from the repository root:

    uv run python -m examples.dynamic_dag_builder_agent
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
    builder_code = '''
dag = dagent.Dag("research", name="Research")
lookup = dagent.Node(
    "lookup",
    target="tool.search",
    inputs={"q": dag.input.request},
)
dag.add_node(lookup)
dag.output = lookup.output
'''
    provider = MockProvider([
        ChatResponse(content=json.dumps({
            "action": "propose_plan",
            "builder_code": builder_code,
            "answer": None,
            "rerun_nodes": [],
        })),
        ChatResponse(content=json.dumps({
            "action": "final_answer",
            "builder_code": None,
            "answer": "Report: found:Research dagent.",
            "rerun_nodes": [],
        })),
    ])
    runner = dagent.Runner(
        provider=provider,
        capabilities=[search],
        planner_frontend="sdk_builder",
    )
    agent = dagent.DagAgent(capabilities=["tool.search"])

    result = await runner.run(
        agent,
        messages=[{"role": "user", "content": "Research dagent."}],
    )

    print(result.status)
    print(result.output_text)
    print(result.plan.planner_frontend if result.plan else "")
    runner.close()


if __name__ == "__main__":
    asyncio.run(main())
