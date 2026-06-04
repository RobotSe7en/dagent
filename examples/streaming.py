"""Stream runner events and read the final unified RunResult.

Run from the repository root:

    uv run python -m examples.streaming
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import dagent
from dagent.providers import ChatResponse, MockProvider


async def main() -> None:
    provider = MockProvider([
        ChatResponse(content="<think>checking</think>The answer is ready."),
    ])
    runner = dagent.Runner(
        workspace=Path(__file__).resolve().parents[1],
        provider=provider,
    )
    agent = dagent.ToolAgent(profile="conversation")

    async for event in runner.stream(agent, "Answer when ready."):
        if event.type == "token":
            print(event.content, end="")
        elif event.type == "status":
            print(event.message)
        elif event.type == "trace" and event.trace is not None:
            print(event.trace.status)
        elif event.type == "review" and event.review is not None:
            print(event.review.message)
        elif event.type == "done" and event.result is not None:
            print()
            print(event.result.kind)
            print(event.result.output_text)
            print(event.result.model_dump(mode="json"))
        elif event.type == "error":
            print(event.message)

    runner.close()


if __name__ == "__main__":
    asyncio.run(main())
