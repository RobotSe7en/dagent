"""Register a subagent and expose it to a top-level ToolAgent.

Run from the repository root:

    uv run python -m examples.agent_delegation
"""

from __future__ import annotations

import asyncio

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


async def main() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="agent_helper",
                        arguments={"prompt": "Summarize the request."},
                    )
                ]
            ),
            ChatResponse(content="The helper summarized the request."),
            ChatResponse(content="Final answer using the helper summary."),
        ]
    )
    runner = dagent.Runner(
        workspace="agent-workspace",
        runtime_directory=".runtime",
        provider=provider,
    )
    helper = dagent.ToolAgent(
        profile="conversation",
        name="helper",
        capabilities=[],
        skills=[],
        max_steps=1,
        description="Summarizes work for the top-level agent.",
    )
    runner.add_agent(helper)

    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=[],
        skills=[],
        agents=["agent.helper"],
    )
    result = await runner.run(
        agent,
        input="Delegate this summary.",
    )

    print(result.status)
    print(result.output_text)
    print([tool["function"]["name"] for tool in provider.requests[0]["tools"]])
    runner.close()


if __name__ == "__main__":
    asyncio.run(main())
