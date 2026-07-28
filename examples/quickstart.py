"""Dump native typed run events from the quickstart agent.

Run from the repository root:

    uv run python -m examples.quickstart
"""

from __future__ import annotations

import asyncio

import dagent
from dagent.providers import Provider
from dagent.profiles import AgentProfile


async def main() -> None:
    provider = Provider(
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        api_key_env="API_KEY",
    )
    profile = AgentProfile(
        name="qa",
        content="You are a helpful assistant.",
    )
    agent = dagent.ToolAgent(profile=profile)
    runner = dagent.Runner(
        workspace="agent-workspace",
        runtime_directory=".runtime",
        provider=provider,
    )
    async for event in runner.stream(
        agent,
        input="当前目录有哪些文件？",
    ):
        print(event)
    runner.close()


if __name__ == "__main__":
    asyncio.run(main())
