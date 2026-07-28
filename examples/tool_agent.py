"""Run a profile-backed ToolAgent with a Python function tool.

Run from the repository root:

    uv run python -m examples.tool_agent
"""

from __future__ import annotations

import asyncio

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


@dagent.tool
def echo(text: str) -> str:
    """Echo text back to the agent."""

    return f"echo:{text}"


async def main() -> None:
    provider = MockProvider(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="tool_echo",
                        arguments={"text": "hello"},
                    )
                ]
            ),
            ChatResponse(content="The echo tool returned hello."),
        ]
    )
    runner = dagent.Runner(
        provider=provider,
        capabilities=[echo],
    )
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["tool.echo"],
    )

    result = await runner.run(
        agent,
        input="Use echo to respond with hello.",
    )

    print(result.status)
    print(result.output_text)
    print([tool["function"]["name"] for tool in provider.requests[0]["tools"]])
    runner.close()


if __name__ == "__main__":
    asyncio.run(main())
