"""Receive requests, answer asynchronously, and continue the original tool.

Run: uv run python -m examples.user_interactions
Uses only the public SDK, an offline provider, and an example host journal.
"""

from __future__ import annotations

import asyncio
from contextlib import closing, suppress
from pathlib import Path
from tempfile import TemporaryDirectory

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall
from examples.conversation_changes import HostJournal


@dagent.tool(supports_context=True)
async def compose(*, context: dagent.ToolContext, callbacks=None) -> str:
    """Keep local state while asking the user twice in one invocation."""
    prefix = "Draft by "
    name = await context.ui.input("Author", initial="Ada")
    try:
        # A caller timeout cancels only this await; the tool can continue.
        await asyncio.wait_for(context.ui.confirm("Optional revision"), timeout=30)
    except (dagent.UserInteractionCancelled, TimeoutError):
        return prefix + name + "; optional revision cancelled"
    return prefix + name


async def main() -> None:
    provider = MockProvider([
        ChatResponse(tool_calls=[
            ToolCall(id="confirm", name="tool_ui_confirm", arguments={"title": "Continue?"}),
            ToolCall(id="select", name="tool_ui_select", arguments={
                "title": "Plan", "options": [{"id": "simple", "label": "Simple plan"}]}),
            ToolCall(id="input", name="tool_ui_input", arguments={"title": "Optional name"}),
            ToolCall(id="editor", name="tool_ui_editor", arguments={
                "title": "HTML draft", "content": "<p>Original</p>", "format": "html"}),
            ToolCall(id="compose", name="tool_compose"),
        ]),
        ChatResponse(content="The original batch completed with the user's results."),
        ChatResponse(tool_calls=[ToolCall(id="saved_edit", name="tool_ui_editor", arguments={
            "title": "Saved draft", "content": "Original", "format": "markdown"})]),
        ChatResponse(content="The edited result was acknowledged before this model call."),
    ])
    requests: asyncio.Queue[dagent.UserInteractionRequest] = asyncio.Queue()
    with TemporaryDirectory(prefix="dagent-interactions-") as workspace:
        with closing(dagent.Runner(provider=provider, workspace=workspace, skill_roots=[],
                result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024))) as runner:
            def ended(event: dagent.UserInteractionEnded) -> None:
                print(f"wait ended: {event.invocation_id} / {event.reason}")

            options = dagent.UserInteractionOptions(requests.put_nowait, ended, timeout_seconds=None)
            agent = dagent.ToolAgent(profile="conversation", capabilities=[*dagent.ui_tools(), compose, "tool.read_file"])

            async def host() -> None:
                while True:
                    request = await requests.get()
                    print(f"request: {request.type} / {request.parameters.title}")
                    identity = dict(run_id=request.run_id, invocation_id=request.invocation_id,
                                    interaction_id=request.interaction_id)
                    if request.parameters.title == "Optional revision":
                        response = dagent.UserInteractionAbandonment(**identity)
                    else:
                        if request.type == "confirm":
                            value = False
                        elif request.type == "select":
                            value = request.parameters.options[0].id
                        elif request.type == "input":
                            value = "Ada" if request.parameters.title == "Author" else ""
                        else:
                            value = ("# Edited draft\n" * 500 if request.parameters.title == "Saved draft"
                                     else "<p>Edited by the host</p>")
                        response = dagent.UserInteractionAnswer(**identity, value=value)
                    await runner.respond_to_interaction(response)
                    print(f"response accepted: {request.invocation_id} (persistence not yet confirmed)")

            worker = asyncio.create_task(host())
            try:
                result = await runner.run(agent, input="Ask the host", interactions=options)
                print(result.status, result.output_text)
                assert "Draft by Ada" in provider.requests[1]["messages"][-1]["content"]

                journal = HostJournal(Path(workspace) / "host-journal", runner)
                async for event in runner.stream(agent, input="Edit and save", interactions=options,
                        persistence=dagent.StreamPersistenceOptions()):
                    if event.type == "conversation.changed":
                        await journal.save(event.data)
                        await runner.acknowledge_changes(event.run_id, event.data.batch_id)
                        for observation in event.data.observations:
                            if observation.type == "tool_execution" and observation.result_item_id:
                                print(f"result persisted: {observation.call_id} / {observation.result_item_id}")
                print("persistent run:", journal.terminal.status)
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker


if __name__ == "__main__":
    asyncio.run(main())
