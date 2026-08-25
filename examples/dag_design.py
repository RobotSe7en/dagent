"""Create a validated DAG candidate without executing its tool.

Run from the repository root:

    uv run python -m examples.dag_design
"""

from __future__ import annotations

import asyncio
import json

import dagent
from dagent.providers import ChatResponse, MockProvider


tool_calls = 0


@dagent.tool
def summarize(text: str) -> str:
    """Summarize text."""

    global tool_calls
    tool_calls += 1
    return text[:80]


async def main() -> None:
    candidate = dagent.Dag("summary", name="Summary", input=str)
    node = dagent.Node(
        "summarize",
        target=summarize,
        inputs={"text": candidate.input},
    )
    candidate.add_node(node)
    candidate.output = node.output
    candidate_spec = candidate.to_dag_spec()

    provider = MockProvider(
        [
            ChatResponse(
                reasoning_content="Choosing the smallest valid graph.",
                content=json.dumps(
                    {
                        "action": "propose_plan",
                        "candidate_json": json.dumps(
                            candidate_spec.model_dump(mode="json")
                        ),
                        "answer": None,
                        "summary": "Created a one-step summary DAG.",
                    }
                )
            )
        ]
    )
    runner = dagent.Runner(
        workspace="agent-workspace",
        runtime_directory=".runtime",
        provider=provider,
        capabilities=[summarize],
    )

    def observe(event: dagent.RunStreamEvent) -> None:
        if event.type == "response.reasoning.delta":
            print(event.data.delta)
        elif event.type.startswith("validation."):
            print(event.type)

    result = await runner.design_dag(
        "Create a DAG that summarizes its string input.",
        on_event=observe,
    )

    if not isinstance(result, dagent.DAGDesignProposal):
        raise RuntimeError(result.diagnostics)
    print(result.type)
    print(result.candidate.id)
    print(tool_calls)
    runner.close()


if __name__ == "__main__":
    asyncio.run(main())
