"""Stream runner chunks and read the final unified RunResult.

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

    async for chunk in runner.stream(agent, "Answer when ready.", text_stream="content"):
        if chunk.text:
            print(chunk.text, end="")
        if chunk.review:
            print(chunk.review.message)
        if chunk.result:
            print()
            print(chunk.result.kind)
            print(chunk.result.output_text)
            print(chunk.result.model_dump(mode="json"))

    runner.close()


if __name__ == "__main__":
    asyncio.run(main())
