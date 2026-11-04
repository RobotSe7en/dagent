"""Oversized tool output overflows to an unchecked file only for read_file models."""

from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
import sys

import pytest

import dagent
from dagent.capabilities.tools.shell_tools import shell
from dagent.harness_runtime.context import ContextAssembler
from dagent.harness_runtime.result_storage import ResultStore, normalize_capability_result
from dagent.providers import ChatResponse, MockProvider
from dagent.schemas import CapabilityInvocation, CapabilityResult
from dagent.schemas.context import ContextPolicy
from dagent.schemas.conversation import (
    AssistantMessage, ContentReference, ConversationState, ToolCallItem, ToolResultMessage, inline_content,
)
from tests.planner_helpers import capability_plan_response, final_answer_response


def _shell(tmp_path, command, *, files):
    return shell(command, cwd=tmp_path, _dagent_result_context={
        "workspace": str(tmp_path), "runtime_directory": ".runtime",
        "max_bytes": 64 * 1024 * 1024, "files": files,
    })


def _python(code):
    return f"\"{sys.executable}\" -c \"{code}\""


@pytest.mark.parametrize("files", [False, True])
def test_small_shell_output_is_inline_and_complete(tmp_path, files):
    output = _shell(tmp_path, _python("print('hello')"), files=files)
    assert "hello" in output.content
    assert output.content_reference is None
    assert output.retention["source_completeness"] == "complete"
    assert not (tmp_path / ".runtime").exists()


@pytest.mark.parametrize("code,complete", [
    ("print('x' * 200000); print('END')", "x" * 200000 + "\nEND\n"),
    # Short lines stay under the byte limit but exceed the line limit.
    ("[print(index) for index in range(3000)]; print('END')", "\n2999\nEND\n"),
], ids=["bytes", "lines"])
def test_shell_output_beyond_inline_limits_overflows_to_an_unchecked_file(tmp_path, code, complete):
    output = _shell(tmp_path, _python(code), files=True)
    reference = output.content_reference
    assert reference is not None
    assert "sha256" not in reference
    saved = (tmp_path / reference["path"]).read_text()
    assert saved.startswith("exit_code=0\n[stdout]\n")
    assert complete in saved
    assert "END" in output.content and "[TRUNCATED]" in output.content
    assert output.retention["source_completeness"] == "complete"
    assert reference["byte_length"] == (tmp_path / reference["path"]).stat().st_size


@pytest.mark.parametrize("code", ["print('x' * 200000); print('END')",
                                  "[print(index) for index in range(3000)]; print('END')"],
                         ids=["bytes", "lines"])
def test_shell_output_without_read_file_keeps_only_a_marked_tail(tmp_path, code):
    output = _shell(tmp_path, _python(code), files=False)
    assert output.content_reference is None
    assert "END" in output.content
    assert output.retention["source_completeness"] == "partial"
    assert output.retention["source_reason"] == "capture_limit"
    assert not (tmp_path / ".runtime").exists()


def test_normalized_display_text_stays_inline_without_read_file(tmp_path):
    invocation = CapabilityInvocation(capability_id="tool.report", kind="tool")
    text = "report " * 1000
    result = CapabilityResult(invocation_id=invocation.invocation_id, capability_id="tool.report", kind="tool",
                              status="completed", content=text, stdout=text, value=b"binary")
    policy = dagent.ResultStoragePolicy(max_inline_bytes=1024)
    inline = normalize_capability_result(result, workspace_path=tmp_path, runtime_directory=".runtime",
                                         policy=policy, result_files=False)
    assert inline.content == inline_content(text)
    assert inline.result.stdout == text
    # Values are data and are stored by size either way, without a digest.
    assert inline.value_reference is not None and inline.value_reference.sha256 is None
    saved = normalize_capability_result(result, workspace_path=tmp_path, runtime_directory=".runtime",
                                        policy=policy)
    assert isinstance(saved.content, ContentReference) and saved.content.sha256 is None
    assert (tmp_path / saved.content.path).read_text() == text


@pytest.mark.asyncio
async def test_truncate_mode_shortens_without_writing_files(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    original = "evidence " * 4000
    conversation = ConversationState(items=(
        AssistantMessage(tool_calls=(ToolCallItem(id="call", name="tool_data"),)),
        ToolResultMessage(call_id="call", name="tool_data", status="completed", content=inline_content(original)),
    ))
    prepared = await ContextAssembler().prepare(
        system_message={"content": "Validate"}, conversation=conversation,
        policy=ContextPolicy(max_tool_result_tokens=128), result_store=store, result_overflow="truncate",
    )
    shown = prepared.messages[-1]["content"]
    assert "[TRUNCATED]" in shown
    assert "[RECOVERY_UNAVAILABLE: no saved original]" in shown
    assert prepared.conversation == conversation
    assert not (tmp_path / ".runtime").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("read_file", [False, True])
async def test_dag_agent_overflows_node_output_only_with_read_file(tmp_path, read_file):
    original = "node evidence " * 2000

    @dagent.tool
    def report() -> str:
        return original

    provider = MockProvider([
        ChatResponse(content=capability_plan_response("tool.report", {}, node_id="report")),
        ChatResponse(content=final_answer_response("done")),
    ])
    capabilities = [report, "tool.read_file"] if read_file else [report]
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[],
                               result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024))) as runner:
        result = await runner.run(dagent.DagAgent(capabilities=capabilities), input="report",
                                  workspace_path=tmp_path)
    assert result.status == "completed"
    call = result.trace.dag_node_traces()["report"].children[0]
    planner = str(provider.requests[-1]["messages"])
    if read_file:
        reference = call.capability_execution.result.content_reference
        assert reference is not None and reference.sha256 is None
        assert (tmp_path / reference.path).read_text() == original
        assert f"path={reference.path}" in planner
    else:
        assert call.capability_execution.result.content_reference is None
        assert original.strip() in planner
        assert "[TRUNCATED]" not in planner and "path=" not in planner
        assert not (tmp_path / ".runtime").exists()


def test_static_dag_reads_an_externalized_value_as_it_is_now(tmp_path):
    body = {"rows": list(range(1000))}
    seen = []

    @dagent.tool
    def produce() -> dict:
        return body

    @dagent.tool
    def consume(data: dict) -> int:
        seen.append(data)
        return len(data["rows"])

    graph = dagent.Dag("values")
    source = dagent.Node("source", target=produce)
    target = dagent.Node("target", target=consume, inputs={"data": source.output})
    graph.add_node(source)
    graph.add_node(target)
    graph.add_edge(source, target)
    with closing(dagent.Runner(workspace=tmp_path, provider=MockProvider([]),
                               result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024))) as runner:
        result = asyncio.run(runner.run(graph))
    assert result.status == "completed"
    reference = result.trace.root.children[0].value_reference
    assert reference is not None and reference.sha256 is None
    assert seen == [body]
    path = Path(result.workspace_path) / reference.path
    assert path.is_file()


def _saved_result(store, text, *, call_id="call"):
    saved = store.save_text(call_id, text)
    return ConversationState(items=(
        AssistantMessage(tool_calls=(ToolCallItem(id=call_id, name="tool_data"),)),
        ToolResultMessage(call_id=call_id, name="tool_data", status="completed", content=saved),
    )), saved


def test_frozen_display_of_a_saved_result_states_no_source_range(tmp_path):
    from dagent.harness_runtime.context import HeuristicTokenCounter
    from dagent.harness_runtime.frozen_results import freeze_result_displays

    store = ResultStore(tmp_path, ".runtime")
    conversation, saved = _saved_result(store, "evidence " * 40000)
    frozen = freeze_result_displays(conversation, result_store=store, policy=ContextPolicy(max_tool_result_tokens=4096),
                                    counter=HeuristicTokenCounter(), files=True, total_budget_tokens=2**60)
    display = frozen.items[1].model_display
    # The preview is an excerpt, so its coordinates must not be stored as file ranges.
    assert display.displayed_spans == ()
    assert display.recovery == saved
    assert "Recover received result" not in display.text


@pytest.mark.asyncio
async def test_saved_result_shown_to_a_model_without_read_file_is_marked_unrecoverable(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    conversation, _ = _saved_result(store, "evidence " * 40000)
    prepared = await ContextAssembler().prepare(
        system_message={"content": "Inspect"}, conversation=conversation, policy=ContextPolicy(), result_store=store,
    )
    assert "[RECOVERY_UNAVAILABLE: read_file unavailable]" in prepared.messages[-1]["content"]
    assert prepared.usage.unrecoverable_tool_results == 1


@pytest.mark.asyncio
async def test_observations_sharing_an_id_get_separate_overflow_files(tmp_path):
    from dagent.schemas.conversation import ResultObservation, UserMessage

    store = ResultStore(tmp_path, ".runtime")
    texts = ["first " * 5000, "second " * 5000]
    conversation = ConversationState(items=tuple(
        UserMessage(content="observe", result_observations=(
            ResultObservation(id="dag-output", name="DAG output", status="completed", content=inline_content(text)),
        )) for text in texts
    ))
    prepared = await ContextAssembler().prepare(
        system_message={"content": "Plan"}, conversation=conversation, tools=[{"function": {"name": "tool_read_file"}}],
        policy=ContextPolicy(max_tool_result_tokens=256), result_store=store,
    )
    saved = [item.result_observations[0].content for item in prepared.conversation.items]
    assert saved[0].path != saved[1].path
    assert [store.read_reference(reference).decode() for reference in saved] == texts


@pytest.mark.asyncio
async def test_unreadable_earlier_records_degrade_instead_of_failing(tmp_path):
    from dagent.harness_runtime.compaction import HistoryCompactor
    from dagent.schemas.conversation import UserMessage
    from tests.test_compaction_strategy import record_for, summary_response

    async def chat(request):
        return summary_response(request)

    engine = HistoryCompactor(policy=ContextPolicy(compaction_strategy="chunked"),
                              assembler=ContextAssembler(context_window_tokens=8192),
                              result_store=ResultStore(tmp_path, ".runtime"),
                              system_message={"content": "Keep the task."}, chat=chat)
    first = await engine.compact(None, (UserMessage(id="a", content="first"),), 256)
    (tmp_path / first.compaction_record.path).unlink()
    second = await engine.compact(first, (UserMessage(id="b", content="second"),), 256)
    assert record_for(engine, second).has_unresolved_history
    assert await engine.reuse_compaction(first, (UserMessage(id="c", content="third"),), 256,
                                         record_reference=first.compaction_record) is None


@pytest.mark.asyncio
async def test_reference_fallback_respects_the_read_boundary(tmp_path):
    from dagent.harness_runtime.compaction import CompactionArchiveError, HistoryCompactor
    from dagent.schemas.common import Boundary
    from dagent.schemas.conversation import UserMessage

    async def chat(_request):
        raise AssertionError("fallback makes no model call")

    def engine(boundary):
        return HistoryCompactor(
            policy=ContextPolicy(compaction_strategy="chunked", allow_reference_fallback=True),
            assembler=ContextAssembler(context_window_tokens=8192),
            result_store=ResultStore(tmp_path, ".runtime", read_boundary=boundary),
            system_message={"content": "Keep the task."}, chat=chat, read_available=True)

    item = (UserMessage(id="a", content="evidence"),)
    with pytest.raises(CompactionArchiveError, match="read boundary"):
        await engine(Boundary(allowed_paths=["src"])).reference_fallback(None, item, 256, reason="offline")
    summary = await engine(Boundary(allowed_paths=[".runtime"])).reference_fallback(None, item, 256, reason="offline")
    assert summary.method == "reference_fallback"
