"""Stream typed run events and read the final unified RunResult.

Run from the repository root:

    uv run python -m examples.quickstart
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
    async for event in runner.stream(
        agent,
        messages=[{"role": "user", "content": "当前目录有哪些文件？"}],
    ):
        print(event)
    runner.close()


if __name__ == "__main__":
    asyncio.run(main())
