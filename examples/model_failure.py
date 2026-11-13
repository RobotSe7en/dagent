"""Inspect a failed model turn after a recoverable tool error, entirely offline.

Run: uv run python -m examples.model_failure
"""

from __future__ import annotations

import asyncio

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


@dagent.tool
def lookup(key: str) -> str:
    """Look up a record by key."""
    raise ValueError(f"No record for {key}.")


async def main() -> None:
    duplicate = ToolCall(id="duplicate", name="tool_lookup", arguments={"key": "missing"})
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(
            id="lookup_1", name="tool_lookup", arguments={"key": "missing"},
        )]),
        ChatResponse(reasoning_content=(
            '<tool_call>{"name":"tool_lookup","arguments":{"key":"another"}}</tool_call>'
        )),
        ChatResponse(tool_calls=[duplicate, duplicate]),
        ChatResponse(),
        ChatResponse(content="Recovered with a complete answer."),
    ])
    runner = dagent.Runner(
        workspace="agent-workspace", provider=provider, capabilities=[lookup],
    )
    try:
        result = await runner.run(dagent.ToolAgent(profile="conversation"), input="Find a record.")
        assert result.status == "failed"
        assert result.output_text == ""
        assert len(provider.requests) == 2
        print(result.status, result.error.code)
        for item in result.new_items:
            if isinstance(item, dagent.ToolResultMessage):
                print(item.name, item.status)
        print(result.conversation.items[-1].reasoning)

        invalid = await runner.run(dagent.ToolAgent(profile="conversation"), input="Try again.")
        assert invalid.status == "failed" and invalid.error.code == "invalid_tool_call"
        assert not invalid.conversation.items[-1].tool_calls
        print(invalid.error.code, invalid.trace.root.children[-1].output["provider_details"])

        events = [event async for event in runner.stream(
            dagent.ToolAgent(profile="conversation", response_retries=1), input="Return a complete answer.",
        )]
        discarded = next(event for event in events if event.type == "response.discarded")
        recovered = events[-1].data.result
        assert recovered.status == "completed"
        print(discarded.data.error.code, discarded.data.retry, recovered.output_text)
    finally:
        runner.close()


if __name__ == "__main__":
    asyncio.run(main())
