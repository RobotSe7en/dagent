"""Stream runner chunks and read the final unified RunResult.

Run from the repository root:

    uv run python -m examples.streaming
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import dagent
from dagent.providers import Provider
from dagent.profiles import AgentProfile


async def main() -> None:
    provider = Provider(
        base_url="https://api.minimaxi.com/v1",
        model="MiniMax-M2.1",
        api_key_env="MINIMAX_API_KEY",
    )
    profile = AgentProfile(
        name="qa",
        content="You are a helpful assistant.",
    )
    agent = dagent.ToolAgent(profile=profile)
    runner = dagent.Runner(
        workspace=Path(__file__).resolve().parents[0],
        provider=provider,
    )
    async for chunk in runner.stream(agent, "你好"):
        print(chunk, end="\n\n")
    runner.close()


if __name__ == "__main__":
    asyncio.run(main())
