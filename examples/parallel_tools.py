"""Overlapping tools, progress callbacks and ordered results, without an LLM.

Parallel synchronous tools can use the same callbacks: the runtime delivers them
on the execution event loop while the worker continues running.

Run: uv run python -m examples.parallel_tools
"""

import asyncio
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


async def main() -> None:
    first_started = asyncio.Event()
    second_finished = asyncio.Event()
    finished: list[str] = []
    progress: list[str] = []

    @dagent.tool(parallel_safe=True, supports_context=True)
    async def lookup(topic: str, *, context, callbacks) -> dict[str, str]:
        callbacks.on_token(f"Start {topic}")
        if topic == "first":
            first_started.set()
            await second_finished.wait()
        else:
            await first_started.wait()
            second_finished.set()
        finished.append(topic)
        return {"topic": topic, "answer": "available"}

    @dagent.tool
    def summarize() -> str:
        """An exclusive action that runs after both lookups finish."""
        assert set(finished) == {"first", "second"}
        return "Both lookups finished."

    provider = MockProvider([
        ChatResponse(tool_calls=[
            ToolCall(id=topic, name="tool_lookup", arguments={"topic": topic})
            for topic in ("first", "second")
        ] + [ToolCall(id="summary", name="tool_summarize", arguments={})]),
        ChatResponse(content="Done."),
    ])
    with TemporaryDirectory(prefix="dagent-parallel-") as directory:
        with closing(dagent.Runner(provider=provider, workspace=Path(directory), skill_roots=[])) as runner:
            agent = dagent.ToolAgent(profile="conversation", capabilities=[lookup, summarize], max_tool_concurrency=2)
            result = await asyncio.wait_for(runner.run(agent, input="Look up both topics.", on_token=progress.append), 5)
    messages = [item for item in result.conversation.items if isinstance(item, dagent.ToolResultMessage)]
    assert finished == ["second", "first"]
    assert [item.call_id for item in messages] == ["first", "second", "summary"]
    assert progress[:2] == ["Start first", "Start second"]
    print("Progress:", progress)
    print("Completion order:", finished)
    print("Conversation order:", [item.call_id for item in messages])
    print(result.output_text)


if __name__ == "__main__":
    asyncio.run(main())
