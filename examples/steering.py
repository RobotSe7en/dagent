"""Steer an active root ToolAgent at a cooperative safe point.

Run from the repository root:

    uv run python -m examples.steering
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import dagent
from dagent.providers import ChatResponse
from dagent.providers.base import StructuredOutputFormat


class PausedProvider:
    """Pause the first model call so the example can queue a steer."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        response_format: StructuredOutputFormat | None = None,
    ) -> ChatResponse:
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            await self.release.wait()
            return ChatResponse(content="A stale draft.")
        return ChatResponse(content=f"Adjusted answer: {messages[-1]['content']}")


async def main() -> None:
    provider = PausedProvider()
    runner = dagent.Runner(
        workspace="agent-workspace",
        runtime_directory=".runtime",
        provider=provider,
    )
    events: list[dagent.RunStreamEvent] = []
    run_id = f"steering_example_{uuid4().hex}"

    async def collect() -> None:
        async for event in runner.stream(
            dagent.ToolAgent(profile="conversation"),
            input="Draft a release note.",
            run_id=run_id,
        ):
            events.append(event)
            if event.type.startswith("steer."):
                print(event.type, event.data)

    try:
        run_task = asyncio.create_task(collect())
        await provider.started.wait()
        receipt = await runner.steer(
            run_id,
            "Focus only on the breaking API change.",
        )
        print("queued", receipt.steer_id)
        provider.release.set()
        await run_task
        print(events[-1].data.result.output_text)
    finally:
        runner.close()


if __name__ == "__main__":
    asyncio.run(main())
