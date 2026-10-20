"""Human review instructions survive externalization and frozen context views."""

from contextlib import closing
import json

import pytest

import dagent
from dagent.providers import ToolCall
from tests.test_cache_usage import _completion, _usage
from tests.test_reasoning_mapping import _endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["legacy", "chunked", "cache_aware"])
@pytest.mark.parametrize("stream", [False, True])
async def test_public_review_resume_keeps_feedback_with_externalized_result(tmp_path, strategy, stream):
    source = "received evidence " * 1000
    feedback = "Do not repeat this operation; inspect the new evidence next."

    @dagent.tool(risk="medium")
    def inspect() -> str:
        """Return enough evidence to require externalization."""
        return source

    def respond(request):
        body = json.loads(request.content)
        has_result = any(message["role"] == "tool" for message in body["messages"])
        return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"),
            content="finished" if has_result else "",
            calls=() if has_result else (ToolCall(id="inspect", name="tool_inspect", arguments={}),))

    async with _endpoint(protocol="chat_completions", token_counting="heuristic",
                         context_window_tokens=131072, responder=respond) as (provider, posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[],
                                  result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024))) as runner:
            first = await runner.run(dagent.ToolAgent(
                profile="conversation", capabilities=[inspect, "tool.read_file"], review="careful",
                context=dagent.ContextPolicy(compaction_strategy=strategy,
                    max_tool_result_tokens=2048 if strategy == "legacy" else 64),
            ), input="Inspect the evidence.", workspace_path=tmp_path)
            assert first.status == "awaiting_review"
            checkpoint = dagent.RunCheckpoint.model_validate_json(first.checkpoint.model_dump_json())
            decision = first.review.approve(feedback=feedback)
            if stream:
                events = [event async for event in runner.resume_stream(decision, checkpoint=checkpoint)]
                result = events[-1].data.result
            else:
                result = await runner.resume(decision, checkpoint=checkpoint)
            assert result.status == "completed"
            item = next(item for item in result.conversation.items if isinstance(item, dagent.ToolResultMessage))
            assert isinstance(item.content, dagent.ContentReference)
            wire = json.loads(posts[-1].content)
            tool_text = next(message["content"] for message in wire["messages"] if message["role"] == "tool")
            assert tool_text.count(f"Reviewer feedback: {feedback}") == 1
            if strategy == "legacy":
                assert item.reviewer_feedback is None
                assert "reviewer_feedback" not in item.model_dump()
                assert feedback in item.content.preview
            else:
                assert item.reviewer_feedback == feedback
                assert feedback in item.model_display.text
                assert feedback not in item.content.preview
                assert (tmp_path / item.model_display.recovery.path).read_text() == source
                assert dagent.ConversationState.model_validate_json(result.conversation.model_dump_json()) == result.conversation
