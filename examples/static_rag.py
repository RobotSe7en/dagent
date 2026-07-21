"""Feed retrieval output into a static DAG agent node as reference content.

Run from the repository root:

    uv run python -m examples.static_rag
"""

from __future__ import annotations

import asyncio

import dagent
from dagent.providers import ChatResponse, MockProvider


@dagent.tool
def retrieve(query: str) -> str:
    """Return deterministic evidence with a source identifier."""

    return f"source-1: dagent executes explicit DAGs for {query}"


async def main() -> None:
    provider = MockProvider([
        ChatResponse(content="dagent executes explicit DAGs. [source-1]"),
    ])
    writer = dagent.ToolAgent(
        profile="conversation",
        capabilities=[],
        skills=[],
        max_steps=1,
        name="writer",
    )

    dag = dagent.Dag("static_rag", input=str)
    retrieve_node = dagent.Node(
        "retrieve",
        target=retrieve,
        inputs={"query": dag.input},
    )
    answer_node = dagent.Node(
        "answer",
        target=writer,
        inputs={
            "prompt": dag.input,
            "reference_content": retrieve_node.output,
        },
    )
    dag.add_node(retrieve_node)
    dag.add_node(answer_node)
    dag.add_edge(retrieve_node, answer_node)

    runner = dagent.Runner(provider=provider)
    result = await runner.run(dag, graph_input="What does dagent execute?")

    print(result.status)
    print(result.node_output("answer"))
    runner.close()


if __name__ == "__main__":
    asyncio.run(main())
