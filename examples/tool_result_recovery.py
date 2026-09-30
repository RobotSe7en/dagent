"""Offline result recovery and pagination: python -m examples.tool_result_recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


@dagent.tool
def rows() -> str:
    """Produce a report larger than the model display budget."""
    return "\n".join(f"row {i}: sample report data" for i in range(2000))


async def main() -> None:
    # This example is the host: TemporaryDirectory explicitly cleans up its files.
    with TemporaryDirectory(prefix="dagent-recovery-") as temporary:
        workspace = Path(temporary)
        provider = MockProvider(
            [
                ChatResponse(
                    tool_calls=[ToolCall(id="report", name="tool_rows", arguments={})]
                ),
                ChatResponse(
                    content="Report saved; this agent has no read_file capability."
                ),
            ]
        )
        # Display ceilings can exceed the window: runtime fitting supplies a
        # smaller request-local budget without changing this policy.
        provider.context_window_tokens = 8192
        provider.max_output_tokens = 1024
        runner = dagent.Runner(
            workspace=workspace,
            runtime_directory=".runtime",
            provider=provider,
            capabilities=[rows],
        )
        try:
            result = await runner.run(
                dagent.ToolAgent(
                    profile="conversation", capabilities=["tool.rows"],
                    context=dagent.ContextPolicy(
                        max_tool_result_tokens=32768,
                        max_total_tool_result_tokens=131072,
                    ),
                ),
                input="Produce the report.",
                workspace_path=workspace,
            )
            item = next(
                item
                for item in result.conversation.items
                if isinstance(item, dagent.ToolResultMessage)
            )
            assert isinstance(item.content, dagent.ContentReference)
            reference = item.content
            assert (workspace / reference.path).read_text() == "\n".join(
                f"row {i}: sample report data" for i in range(2000)
            )
            print(result.output_text)
            print("Saved bytes:", reference.byte_length)
            usage = result.context_usage[-1]
            assert usage.estimated_input_tokens <= usage.compaction_trigger_tokens
            assert usage.truncated_tool_results == 1
            assert usage.compaction_method == "none"
            print("Fitted input tokens:", usage.estimated_input_tokens, "/", usage.input_budget_tokens)
        finally:
            runner.close()

        reader = MockProvider(
            [
                ChatResponse(
                    tool_calls=[
                        ToolCall(
                            id="read",
                            name="tool_read_file",
                            arguments={
                                "path": reference.path,
                                "offset_chars": 0,
                                "limit_chars": 8192,
                            },
                        ),
                        ToolCall(
                            id="search",
                            name="tool_grep",
                            arguments={
                                "path": reference.path,
                                "pattern": "row",
                                "offset": 200,
                                "limit": 20,
                            },
                        ),
                        ToolCall(
                            id="list",
                            name="tool_list_files",
                            arguments={
                                "path": ".runtime/results",
                                "offset": 0,
                                "limit": 10,
                            },
                        ),
                        ToolCall(
                            id="shell",
                            name="tool_shell",
                            arguments={
                                "command": f'"{sys.executable}" -c "print(\'log line\\n\'*500)"'
                            },
                        ),
                    ]
                ),
                ChatResponse(
                    content="Read a character window, searched a page and saved shell output."
                ),
            ]
        )
        runner = dagent.Runner(
            workspace=workspace, runtime_directory=".runtime", provider=reader
        )
        try:
            result = await runner.run(
                dagent.ToolAgent(
                    profile="conversation",
                    context=dagent.ContextPolicy(
                        max_tool_result_tokens=256, max_total_tool_result_tokens=2048,
                    ),
                    capabilities=[
                        "tool.read_file",
                        "tool.grep",
                        "tool.list_files",
                        "tool.shell",
                    ],
                ),
                input="Inspect the saved report and a sample command log.",
                workspace_path=workspace,
            )
            print(result.output_text)
            for message in reader.requests[-1]["messages"]:
                if message.get("role") == "tool" and message.get("tool_call_id") == "read":
                    for line in message["content"].splitlines():
                        if line.startswith(("[File window:", "[Displayed:", "[Continue with")):
                            print(line)
            print(
                "Model tool-result tokens:", result.context_usage[-1].tool_result_tokens
            )
        finally:
            runner.close()


if __name__ == "__main__":
    asyncio.run(main())
