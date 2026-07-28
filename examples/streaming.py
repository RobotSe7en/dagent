"""Stream typed run events and read the final unified RunResult.

Run from the repository root:

    uv run python -m examples.streaming
"""

from __future__ import annotations

import asyncio

import dagent
from dagent.providers import ChatResponse, MockProvider


async def main() -> None:
    provider = MockProvider([
        ChatResponse(content="<think>checking</think>The answer is ready."),
    ])
    runner = dagent.Runner(
        workspace="agent-workspace",
        runtime_directory=".runtime",
        provider=provider,
    )
    agent = dagent.ToolAgent(profile="conversation")

    async for event in runner.stream(
        agent,
        input="Answer when ready.",
    ):
        if event.type == "run.started":
            print(f"[{event.run_id}] {event.data.kind} run started")
        elif event.type == "response.content.delta":
            print(event.data.delta, end="")
        elif event.type == "response.finished":
            print()
        elif event.type == "run.finished":
            result = event.data.result
            if result.requires_review:
                print(result.review.message)
            print(result.kind)
            print(result.output_text)
            print(result.model_dump(mode="json"))

    runner.close()


if __name__ == "__main__":
    asyncio.run(main())
