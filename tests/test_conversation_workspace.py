"""Runs of a conversation share one workspace; the SDK carries no files across."""

from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path

import pytest

import dagent
from dagent.harness_runtime.artifacts import conversation_workspace
from dagent.harness_runtime.context import user_content_for_model
from dagent.harness_runtime.result_storage import ResultStore
from dagent.providers import ChatResponse, MockProvider, ToolCall


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("conversation_id", ["conversation_1", "../escape", "a/b\\c d", "会话"])
def test_conversation_workspace_names_are_safe_and_stable(tmp_path, conversation_id):
    first = conversation_workspace(tmp_path, conversation_id)
    again = conversation_workspace(tmp_path, conversation_id)
    assert first == again and first.parent == tmp_path.resolve() and first.is_dir()
    assert first.name.startswith("conversation_") and len(first.name) == len("conversation_") + 32
    assert conversation_workspace(tmp_path, conversation_id + "x", create=False) != first


def test_a_continuation_reads_earlier_overflow_files_in_its_workspace(tmp_path):
    original = "first turn evidence " * 2000

    @dagent.tool
    def report() -> str:
        return original

    def runner(responses):
        return dagent.Runner(provider=MockProvider(responses), workspace=tmp_path, skill_roots=[],
                             result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024))

    agent = dagent.ToolAgent(profile="conversation", capabilities=[report, "tool.read_file"])
    with closing(runner([
        ChatResponse(tool_calls=[ToolCall(id="call_0", name="tool_report", arguments={})]),
        ChatResponse(content="saved"),
    ])) as first_runner:
        first = run(first_runner.run(agent, input="report"))
    saved = next(item for item in first.conversation.items if isinstance(item, dagent.ToolResultMessage)).content
    assert isinstance(saved, dagent.ContentReference)
    # A later turn, even from a new Runner, runs in the same workspace.
    with closing(runner([
        ChatResponse(tool_calls=[ToolCall(id="call_0", name="tool_read_file", arguments={
            "path": saved.path, "offset_chars": 0, "limit_chars": 40})]),
        ChatResponse(content="read"),
    ])) as second_runner:
        second = run(second_runner.run(agent, input="read it again", conversation=first.conversation))
    assert second.workspace_path == first.workspace_path
    read = next(item for item in second.new_items if isinstance(item, dagent.ToolResultMessage))
    assert read.status == "completed" and original[:40] in read.content.text


def test_runs_neither_read_nor_copy_earlier_archives(tmp_path, monkeypatch):
    conversation = dagent.ConversationState()
    workspace = conversation_workspace(tmp_path / "runs", conversation.id)
    store = ResultStore(workspace, ".runtime")
    archive = store.snapshot_text("compacted history " * 1000)
    conversation = conversation.model_copy(update={"summary": dagent.ContextSummary(
        content="Earlier work.", source_item_count=2, compaction_record=archive)})
    reads = []
    read_bytes = Path.read_bytes

    def counted(path):
        reads.append(path)
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted)
    provider = MockProvider([ChatResponse(content="first"), ChatResponse(content="second")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        agent = dagent.ToolAgent(profile="conversation")
        first = run(runner.run(agent, input="Continue.", conversation=conversation))
        # A lost archive does not fail the next run either.
        (workspace / archive.path).unlink()
        second = run(runner.run(agent, input="Continue.", conversation=first.conversation))
    assert first.status == second.status == "completed"
    assert first.workspace_path == second.workspace_path == str(workspace)
    assert not any(".runtime" in path.parts for path in reads)
    assert not list(tmp_path.rglob("conversations"))


def test_static_dag_runs_keep_their_own_run_workspace(tmp_path):
    @dagent.tool
    def produce() -> str:
        return "value"

    graph = dagent.Dag("values")
    graph.add_node(dagent.Node("source", target=produce))
    with closing(dagent.Runner(workspace=tmp_path, provider=MockProvider([]), skill_roots=[])) as runner:
        result = run(runner.run(graph, run_id="static_run"))
    assert Path(result.workspace_path).name == "static_run"


def test_attachment_digest_is_optional_and_only_rendered_when_present():
    new = dagent.Attachment(path="uploads/new.txt", media_type="text/plain", byte_length=5)
    old = dagent.Attachment.model_validate({
        "path": "uploads/old.txt", "media_type": "text/plain", "byte_length": 5, "sha256": "a" * 64,
    })
    assert new.sha256 is None
    text = user_content_for_model(dagent.UserMessage(content="Read.", attachments=(new, old)))
    assert "- uploads/new.txt (text/plain, 5 bytes)\n" in text
    # Records from earlier versions render exactly as before.
    assert f"- uploads/old.txt (text/plain, 5 bytes, sha256={'a' * 64})\n" in text
    with pytest.raises(ValueError):
        dagent.Attachment(path="uploads/bad.txt", byte_length=1, sha256="not-a-digest")


def test_conversation_resource_error_is_no_longer_exported():
    assert not hasattr(dagent, "ConversationResourceError")
    assert "ConversationResourceError" not in dagent.__all__


@pytest.mark.asyncio
async def test_a_later_turn_links_the_earlier_compaction_without_degrading(tmp_path):
    import json

    from dagent.harness_runtime.compaction import CompactionRecordV1
    from tests.test_cache_usage import _completion, _usage
    from tests.test_compaction_strategy import checkpoint, request_aliases
    from tests.test_reasoning_mapping import _endpoint

    steps = summaries = 0
    done_after = [15]

    @dagent.tool
    def query(index: int) -> str:
        """Return one batch of evidence."""
        return f"step={index}; verified=true; " + "evidence " * 180

    def respond(request):
        nonlocal steps, summaries
        body = json.loads(request.content)
        if ("Sources to summarize:" in json.dumps(body, ensure_ascii=False)
                or body.get("messages", [{}])[0].get("content", "").startswith("Produce a task checkpoint")):
            summaries += 1
            raw = "\n".join(str(item.get("content", "")) for item in body["messages"])
            content = checkpoint(requirements=[("Keep collecting evidence.", [request_aliases(raw)[0]])])
            return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"),
                               content=content)
        steps += 1
        done = steps > done_after[0]
        if done:
            done_after[0] += 16
        return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"),
                           content="done" if done else "",
                           calls=() if done else (ToolCall(id=f"call_{steps}", name="tool_query",
                                                           arguments={"index": steps}),))

    async with _endpoint(protocol="chat_completions", token_counting="heuristic", context_window_tokens=8192,
                         max_output_tokens=512, responder=respond) as (provider, _posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            agent = dagent.ToolAgent(profile="conversation", capabilities=[query], max_steps=40,
                                     context=dagent.ContextPolicy(compaction_strategy="cache_aware",
                                                                  summary_max_tokens=512))
            first = await runner.run(agent, input="Collect evidence.")
            earlier = first.conversation.summary.compaction_record
            before = summaries
            second = await runner.run(agent, input="Collect more.", conversation=first.conversation)
    assert first.status == second.status == "completed"
    assert earlier is not None and summaries > before
    # The later turn compacted again in the same workspace, read the earlier
    # record and linked it, so nothing is marked as lost.
    store = ResultStore(second.workspace_path, ".runtime")
    reference = second.conversation.summary.compaction_record
    chain = []
    while reference is not None:
        record = CompactionRecordV1.model_validate_json(store.read_reference(reference))
        chain.append(reference)
        reference = record.previous_record
    assert earlier in chain[1:]
    assert not record.has_unresolved_history
    assert "[DEGRADED_CONTEXT]" not in second.conversation.summary.content
