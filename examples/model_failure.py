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
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(
            id="lookup_1", name="tool_lookup", arguments={"key": "missing"},
        )]),
        ChatResponse(reasoning_content=(
            '<tool_call>{"name":"tool_lookup","arguments":{"key":"another"}}</tool_call>'
        )),
    ])
    runner = dagent.Runner(workspace="agent-workspace", provider=provider, capabilities=[lookup])
    try:
        result = await runner.run(dagent.ToolAgent(profile="conversation"), input="Find a record.")
        assert result.status == "failed"
        assert result.output_text == ""
        print(result.status, result.error.code)
        for item in result.new_items:
            if isinstance(item, dagent.ToolResultMessage):
                print(item.name, item.status)
        print(result.conversation.items[-1].reasoning)
    finally:
        runner.close()


if __name__ == "__main__":
    asyncio.run(main())
