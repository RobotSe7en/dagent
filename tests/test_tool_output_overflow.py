"""Oversized tool output overflows to an unchecked file only for read_file models."""

from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
import re
import sys

import pytest

import dagent
from dagent.capabilities.tools.shell_tools import shell
from dagent.harness_runtime.context import ContextAssembler, compaction_source
from dagent.harness_runtime.result_storage import ResultStore, normalize_capability_result
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.providers.model_io import ModelResponse
from dagent.schemas import CapabilityInvocation, CapabilityResult
from dagent.schemas.context import ContextPolicy
from dagent.schemas.conversation import (
    AssistantMessage, ContentReference, ContextSummary, ConversationState, ResultObservation, ToolCallItem,
    ToolResultMessage, UserMessage, inline_content,
)
from dagent.schemas.retention import ResultCursor, ResultRetention, ResultStorageWarning
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
        assert "[TRUNCATED]" in planner and "[RECOVERY_UNAVAILABLE: no saved original]" in planner
        assert "path=" not in planner
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
@pytest.mark.parametrize("strategy", ["legacy", "cache_aware"])
async def test_saved_result_shown_to_a_model_without_read_file_is_bounded_and_marked_unrecoverable(
        tmp_path, strategy):
    from dagent.harness_runtime.context import HeuristicTokenCounter

    store = ResultStore(tmp_path, ".runtime")
    conversation, saved = _saved_result(store, "evidence " * 40000)
    prepared = await ContextAssembler(context_window_tokens=131072, max_output_tokens=8192).prepare(
        system_message={"content": "Inspect"}, conversation=conversation, result_store=store,
        policy=ContextPolicy(compaction_strategy=strategy, max_tool_result_tokens=128),
        enable_new_strategy=strategy != "legacy",
    )
    shown = (prepared.messages[-1]["content"] if strategy == "legacy"
             else prepared.conversation.items[1].model_display.text)
    # Its stored preview is still shortened to the display limit (cache_aware
    # adds its metadata floor to the body cap).
    assert HeuristicTokenCounter().count_text(saved.preview) > 1000
    assert HeuristicTokenCounter().count_text(shown) <= 256
    assert "[RECOVERY_UNAVAILABLE: read_file unavailable]" in shown
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


def test_files_are_readable_only_when_the_boundary_admits_the_runtime_folders(tmp_path):
    from dagent.schemas.common import Boundary

    def readable(*allowed, directory=".runtime"):
        boundary = Boundary(allowed_paths=list(allowed)) if allowed else None
        return ResultStore(tmp_path / "ws", directory, read_boundary=boundary).files_readable()

    assert readable() and readable(".") and readable(".runtime")
    assert not readable("src")
    # Reference fallback points at archives, so results alone are not enough.
    assert not readable(".runtime/results")
    # An external runtime directory keeps its logical .runtime paths.
    assert readable(".", directory=str(tmp_path / "external"))
    assert not readable("src", directory=str(tmp_path / "external"))
    # Only paths are checked; nothing is created or opened.
    assert not (tmp_path / "ws").exists() and not (tmp_path / "external").exists()


@pytest.mark.parametrize("allowed,saved", [(["src"], False), (["."], True)])
def test_tool_agent_overflows_only_when_the_read_boundary_admits_the_file(tmp_path, allowed, saved):
    from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
    from dagent.capabilities.decorator import tool
    from dagent.capabilities.providers import ToolCapabilityProvider
    from dagent.capabilities.tools.file_tools import create_file_tool_registry
    from dagent.harness_runtime import CapabilityExecutor, ToolAgentLoop
    from dagent.providers import ToolCall
    from dagent.schemas import Boundary, UserMessage

    original = "evidence " * 4000

    @tool
    def report() -> str:
        return original

    catalog = CapabilityCatalog(workspace_root=tmp_path)
    ToolCapabilityProvider(create_file_tool_registry()).register_into(catalog)
    catalog.register(report.definition, report.handler)
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call", name="tool_report", arguments={})]),
        ChatResponse(content="Done."),
    ])
    loop = ToolAgentLoop(
        runtime_directory=".runtime", provider=provider, capability_executor=CapabilityExecutor(catalog),
        tool_adapter=CapabilityToolAdapter(catalog, toolsets=[CapabilityToolset("builtin", tuple(sorted(catalog.ids())))]),
    )
    outcome = asyncio.run(loop.run(
        ConversationState(items=(UserMessage(content="report"),)), boundary=Boundary(allowed_paths=allowed),
        system_message={"role": "system", "content": "Report."}, context_policy=ContextPolicy(),
        result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024), context_assembler=ContextAssembler(),
    ))
    assert outcome.state.status == "completed"
    result = next(item for item in outcome.new_items if isinstance(item, ToolResultMessage))
    shown = str(provider.requests[-1]["messages"])
    if saved:
        assert isinstance(result.content, ContentReference)
        assert f"path={result.content.path}" in shown
    else:
        # The model could not open a file, so the display is shortened without one.
        assert result.content == inline_content(original)
        assert "[TRUNCATED]" in shown and "[RECOVERY_UNAVAILABLE: no saved original]" in shown and "path=" not in shown
        assert not (tmp_path / ".runtime").exists()


@pytest.mark.asyncio
async def test_cache_aware_display_writes_no_file_when_the_boundary_excludes_runtime_files(tmp_path):
    from dagent.schemas.common import Boundary

    original = "evidence " * 4000
    conversation = ConversationState(items=(
        AssistantMessage(tool_calls=(ToolCallItem(id="call", name="tool_data"),)),
        ToolResultMessage(call_id="call", name="tool_data", status="completed", content=inline_content(original)),
    ))

    async def prepare(boundary):
        return await ContextAssembler(context_window_tokens=131072, max_output_tokens=8192).prepare(
            system_message={"content": "Inspect"}, conversation=conversation,
            tools=[{"type": "function", "function": {"name": "tool_read_file"}}],
            policy=ContextPolicy(compaction_strategy="cache_aware", max_tool_result_tokens=256),
            result_store=ResultStore(tmp_path, ".runtime", read_boundary=boundary), enable_new_strategy=True,
        )

    excluded = await prepare(Boundary(allowed_paths=["src"]))
    item = excluded.conversation.items[1]
    assert item.content == inline_content(original) and item.model_display.recovery is None
    assert "[RECOVERY_UNAVAILABLE: no saved original]" in item.model_display.text
    assert excluded.usage.truncated_tool_results == 1
    assert excluded.usage.unrecoverable_tool_results == 1
    assert not (tmp_path / ".runtime").exists()
    admitted = await prepare(Boundary(allowed_paths=["."]))
    assert admitted.conversation.items[1].model_display.recovery is not None
    assert admitted.usage.truncated_tool_results == 1


def test_emergency_transition_needs_read_access_even_for_saved_results(tmp_path):
    from dagent.harness_runtime.frozen_results import reference_frozen_result_displays
    from dagent.schemas.conversation import FrozenResultDisplay

    store = ResultStore(tmp_path, ".runtime")
    conversation, saved = _saved_result(store, "evidence " * 40000)
    result = conversation.items[1]
    display = FrozenResultDisplay(text="[status=completed]\nevidence evidence", recovery=saved)
    conversation = conversation.model_copy(update={"items": (
        conversation.items[0], result.model_copy(update={"model_display": display}))})
    # Without read access the reference-only display would drop text the
    # model can never get back, so nothing changes.
    unchanged = reference_frozen_result_displays(conversation, result_store=store, files=False,
                                                 result_ids={result.id})
    assert unchanged is conversation
    changed = reference_frozen_result_displays(conversation, result_store=store, files=True,
                                               result_ids={result.id})
    assert changed.items[1].model_display.revision == 1


NUL_TEXT = "binary\0data " * 4000


def test_text_with_nul_never_overflows_to_a_file(tmp_path):
    invocation = CapabilityInvocation(capability_id="tool.dump", kind="tool")
    result = CapabilityResult(invocation_id=invocation.invocation_id, capability_id="tool.dump", kind="tool",
                              status="completed", content=NUL_TEXT, stdout=NUL_TEXT)
    normalized = normalize_capability_result(result, workspace_path=tmp_path, runtime_directory=".runtime",
                                             policy=dagent.ResultStoragePolicy(max_inline_bytes=1024))
    # read_file rejects text with NUL, so a file would be unreadable.
    assert normalized.content == inline_content(NUL_TEXT)
    assert normalized.result.stdout == NUL_TEXT
    assert not (tmp_path / ".runtime").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["legacy", "cache_aware"])
async def test_inline_results_with_nul_are_shortened_without_a_file(tmp_path, strategy):
    store = ResultStore(tmp_path, ".runtime")
    conversation = ConversationState(items=(
        AssistantMessage(tool_calls=(ToolCallItem(id="call", name="tool_data"),)),
        ToolResultMessage(call_id="call", name="tool_data", status="completed", content=inline_content(NUL_TEXT)),
    ))
    prepared = await ContextAssembler(context_window_tokens=131072, max_output_tokens=8192).prepare(
        system_message={"content": "Inspect"}, conversation=conversation,
        tools=[{"type": "function", "function": {"name": "tool_read_file"}}],
        policy=ContextPolicy(compaction_strategy=strategy, max_tool_result_tokens=256),
        result_store=store, enable_new_strategy=strategy != "legacy",
    )
    item = prepared.conversation.items[1]
    # read_file could not open a file holding it, so none is written; the
    # display is still bounded, as for a model that cannot open files.
    assert item.content == inline_content(NUL_TEXT)
    shown = (prepared.messages[-1]["content"] if strategy == "legacy" else item.model_display.text)
    assert "[TRUNCATED]" in shown and "[RECOVERY_UNAVAILABLE: no saved original]" in shown
    assert "path=" not in shown
    assert prepared.usage.truncated_tool_results == 1
    assert prepared.usage.unrecoverable_tool_results == 1
    assert not (tmp_path / ".runtime").exists()
    if strategy == "cache_aware":
        from dagent.harness_runtime.frozen_results import reference_frozen_result_displays

        emergency = reference_frozen_result_displays(prepared.conversation, result_store=store, files=True,
                                                     result_ids={item.id})
        assert emergency is prepared.conversation
        assert not (tmp_path / ".runtime").exists()


def test_shell_output_with_nul_keeps_only_a_marked_tail(tmp_path):
    output = _shell(tmp_path, _python("import sys; sys.stdout.write('x\\\\0y' * 100000); print('END')"), files=True)
    assert output.content_reference is None
    assert "END" in output.content
    assert output.retention["source_completeness"] == "partial"
    results = tmp_path / ".runtime" / "results"
    assert not results.exists() or not any(results.iterdir())


@pytest.mark.asyncio
async def test_failed_overflow_write_counts_as_truncated_and_unrecoverable(tmp_path, monkeypatch):
    store = ResultStore(tmp_path, ".runtime")

    def fail(self, reference, text):
        raise OSError("disk full")

    monkeypatch.setattr(ResultStore, "write_text", fail)
    conversation = ConversationState(items=(
        AssistantMessage(tool_calls=(ToolCallItem(id="call", name="tool_data"),)),
        ToolResultMessage(call_id="call", name="tool_data", status="completed",
                          content=inline_content("evidence " * 4000)),
    ))
    with pytest.warns(RuntimeWarning):
        prepared = await ContextAssembler(context_window_tokens=131072, max_output_tokens=8192).prepare(
            system_message={"content": "Inspect"}, conversation=conversation,
            tools=[{"type": "function", "function": {"name": "tool_read_file"}}],
            policy=ContextPolicy(compaction_strategy="cache_aware", max_tool_result_tokens=256),
            result_store=store, enable_new_strategy=True,
        )
    display = prepared.conversation.items[1].model_display
    assert display.recovery is None and "[RECOVERY_UNAVAILABLE: no saved original]" in display.text
    assert prepared.usage.truncated_tool_results == 1
    assert prepared.usage.unrecoverable_tool_results == 1


def test_repeated_invocation_ids_never_overwrite_earlier_result_files(tmp_path):
    # A ToolAgent uses the provider's call id, which many servers repeat.
    policy = dagent.ResultStoragePolicy(max_inline_bytes=1024)
    saved = []
    for text in ("first " * 1000, "second " * 1000):
        result = CapabilityResult(invocation_id="call_0", capability_id="tool.report", kind="tool",
                                  status="completed", content=text, stdout=text, value={"rows": [text]})
        saved.append((text, normalize_capability_result(result, workspace_path=tmp_path,
                                                        runtime_directory=".runtime", policy=policy)))
    paths = [{ref.path for ref in normalized.references} for _, normalized in saved]
    assert len(paths[0]) == 3 and not paths[0] & paths[1]
    for text, normalized in saved:
        assert (tmp_path / normalized.content.path).read_text() == text


def test_runs_reusing_a_workspace_and_call_id_keep_their_own_result_files(tmp_path):
    from dagent.providers import ToolCall

    texts = iter(["first run " * 2000, "second run " * 2000])

    @dagent.tool
    def report() -> str:
        return next(texts)

    references = []
    with closing(dagent.Runner(workspace=tmp_path, skill_roots=[], provider=MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_0", name="tool_report", arguments={})]),
        ChatResponse(content="done"),
        ChatResponse(tool_calls=[ToolCall(id="call_0", name="tool_report", arguments={})]),
        ChatResponse(content="done"),
    ]), result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024))) as runner:
        agent = dagent.ToolAgent(profile="conversation", capabilities=[report, "tool.read_file"])
        for _ in range(2):
            result = asyncio.run(runner.run(agent, input="report", workspace_path=tmp_path))
            references.append(next(item for item in result.conversation.items
                                   if isinstance(item, ToolResultMessage)).content)
    assert references[0].path != references[1].path
    assert (tmp_path / references[0].path).read_text() == "first run " * 2000
    assert (tmp_path / references[1].path).read_text() == "second run " * 2000


def test_display_limit_bounds_results_for_a_small_window_without_read_file(tmp_path):
    from dagent.providers import ToolCall

    original = "evidence row " * 2500  # About 30 KB.

    @dagent.tool
    def report() -> str:
        return original

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_0", name="tool_report", arguments={})]),
        ChatResponse(content="done"),
    ])
    provider.context_window_tokens = 4096
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = asyncio.run(runner.run(dagent.ToolAgent(
            profile="conversation", capabilities=[report],
            context=ContextPolicy(max_tool_result_tokens=256),
        ), input="report", workspace_path=tmp_path))
    # The result is shortened in memory instead of overflowing the window.
    assert result.status == "completed"
    shown = next(message["content"] for message in provider.requests[-1]["messages"] if message["role"] == "tool")
    assert "[TRUNCATED]" in shown and "[RECOVERY_UNAVAILABLE: no saved original]" in shown
    assert result.state.context_usage[-1].compacted_items == 0
    assert not (tmp_path / ".runtime").exists()


@pytest.mark.asyncio
async def test_value_file_does_not_make_shortened_text_recoverable(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    value = store.save_text("value", '{"rows": []}', field="value")
    conversation = ConversationState(items=(
        AssistantMessage(tool_calls=(ToolCallItem(id="call", name="tool_data"),)),
        ToolResultMessage(call_id="call", name="tool_data", status="completed",
                          content=inline_content(NUL_TEXT), value_reference=value),
    ))
    prepared = await ContextAssembler().prepare(
        system_message={"content": "Inspect"}, conversation=conversation,
        tools=[{"type": "function", "function": {"name": "tool_read_file"}}],
        policy=ContextPolicy(max_tool_result_tokens=256), result_store=store,
    )
    shown = prepared.messages[-1]["content"]
    # The value file is listed, but it does not hold the omitted text.
    assert f"path={value.path}" in shown
    assert "[RECOVERY_UNAVAILABLE: no saved original]" in shown
    assert prepared.usage.unrecoverable_tool_results == 1


@pytest.mark.asyncio
async def test_planner_without_read_file_sees_saved_observations_bounded_and_unreadable(tmp_path):
    from dagent.harness_runtime.context import HeuristicTokenCounter
    from dagent.schemas.conversation import ResultObservation, UserMessage

    store = ResultStore(tmp_path, ".runtime")
    saved = store.save_text("node", "node evidence " * 40000)
    conversation = ConversationState(items=(UserMessage(content="Plan", result_observations=(
        ResultObservation(id="node", name="DAG output", status="completed", content=saved),
    )),))
    # A DagAgent planner without tool.read_file uses the "truncate" overflow.
    prepared = await ContextAssembler().prepare(
        system_message={"content": "Plan"}, conversation=conversation, result_store=store,
        policy=ContextPolicy(max_tool_result_tokens=128), result_overflow="truncate",
    )
    shown = prepared.messages[-1]["content"]
    assert HeuristicTokenCounter().count_text(saved.preview) > 1000
    assert "[TRUNCATED]" in shown and "[RECOVERY_UNAVAILABLE: read_file unavailable]" in shown
    assert prepared.usage.unrecoverable_tool_results == 1


@pytest.mark.parametrize("separator", ["\\\\r", "\\\\r\\\\n", "\\\\u2028"], ids=["cr", "crlf", "line_separator"])
def test_shell_lines_split_by_any_line_break_overflow_to_a_complete_file(tmp_path, separator):
    # The display counts lines like str.splitlines(); capture must count the
    # same breaks, or lines past the limit are cut without being saved.
    code = f"import sys; sys.stdout.write(''.join('line ' + str(i) + '{separator}' for i in range(3000)))"
    output = _shell(tmp_path, _python(code), files=True)
    reference = output.content_reference
    assert reference is not None
    saved = (tmp_path / reference["path"]).read_text(encoding="utf-8")
    assert saved.splitlines()[2] == "line 0" and "line 2999" in saved
    assert output.retention["source_completeness"] == "complete"


READ_FILE_TOOL = {"type": "function", "function": {"name": "tool_read_file"}}


def _long_result(index, rows=1500):
    # Longer than the display limit and than the preview a saved result carries.
    return f"FACT_{index}=value{index}\n" + f"row {index} evidence\n" * rows + f"END_{index}"


async def _legacy_summary_sources(store, texts, *, tools, shape="runs", retention=None, policy=ContextPolicy()):
    """Prepare a legacy context over ``texts`` and return it with the text of each summary request.

    ``shape`` is "runs" (one earlier run per tool result) or "planner" (one
    run whose observations carry the results, like a DagAgent planner thread).
    """

    items = []
    for index, text in enumerate(texts, start=1):
        if shape == "runs":
            items += [
                UserMessage(run_id=f"run_{index}", content=f"Collect page {index}."),
                AssistantMessage(run_id=f"run_{index}", tool_calls=(ToolCallItem(id=f"call_{index}", name="tool_data"),)),
                ToolResultMessage(run_id=f"run_{index}", call_id=f"call_{index}", name="tool_data",
                                  status="completed", content=inline_content(text), retention=retention),
            ]
        else:
            items += [
                AssistantMessage(run_id="active", content=f"Plan {index}."),
                UserMessage(run_id="active", content=f"Observation {index}.", result_observations=(
                    ResultObservation(id=f"node_{index}", name="node", status="completed",
                                      content=inline_content(text), retention=retention),)),
            ]
    task = UserMessage(run_id="active", content="Continue.")
    items = [*items, task] if shape == "runs" else [task, *items]
    sources = []

    async def compact(previous, compacted, limit):
        sources.append(compaction_source(previous, compacted))
        return ContextSummary(content="summary", source_item_count=len(compacted))

    prepared = await ContextAssembler(context_window_tokens=16384, max_output_tokens=512).prepare(
        system_message={"content": "Inspect"}, conversation=ConversationState(items=tuple(items)),
        tools=tools, policy=policy, result_store=store, compact=compact, active_run_id="active",
    )
    return prepared, sources


@pytest.mark.parametrize("shape", ["runs", "planner"])
@pytest.mark.asyncio
async def test_legacy_summary_reads_long_results_alike_with_and_without_read_file(tmp_path, shape):
    texts = [_long_result(index) for index in range(1, 7)]
    saved, saved_sources = await _legacy_summary_sources(
        ResultStore(tmp_path / "saved", ".runtime"), texts, tools=[READ_FILE_TOOL], shape=shape)
    inline, inline_sources = await _legacy_summary_sources(
        ResultStore(tmp_path / "inline", ".runtime"), texts, tools=[], shape=shape)
    # Without read_file the shortened results stay inline, in full.
    assert not any(isinstance(result.content, ContentReference)
                   for item in inline.conversation.items
                   for result in ([item] if isinstance(item, ToolResultMessage)
                                  else getattr(item, "result_observations", ())))
    # The summary still reads the bounded preview a saved result carries, so
    # long results do not push the rest of the history out of its request.
    assert inline_sources and inline_sources == [
        "\n\n".join(part for part in source.split("\n\n") if not part.startswith("Stored result: "))
        for source in saved_sources
    ]
    assert all(text not in source for source in inline_sources for text in texts)
    # The retention estimate reads the same text, so the same turns are kept.
    kept = [(type(item).__name__, item.run_id) for item in inline.conversation.items]
    assert kept == [(type(item).__name__, item.run_id) for item in saved.conversation.items]
    if shape == "runs":
        assert ("ToolResultMessage", "run_6") in kept


@pytest.mark.parametrize("case", ["shown_completely", "no_result_store", "file_window", "storage_warning"])
@pytest.mark.asyncio
async def test_legacy_summary_keeps_the_full_text_of_results_that_are_never_saved(tmp_path, case):
    # As in 0.10.4, these results contribute their whole text to the summary.
    texts = [_long_result(index) for index in range(1, 7)]
    store, retention, policy = ResultStore(tmp_path, ".runtime"), None, ContextPolicy()
    if case == "shown_completely":
        policy = ContextPolicy(max_tool_result_tokens=8192)
    elif case == "no_result_store":
        store = None
    elif case == "file_window":
        retention = ResultRetention(window_start=0, window_length=len(texts[0]), continuation=ResultCursor(
            tool="read_file", path="notes.txt", offset=len(texts[0])))
    else:
        retention = ResultRetention(storage_warnings=(
            ResultStorageWarning(field="content", error_type="OSError", message="disk full"),))
    _, sources = await _legacy_summary_sources(store, texts, tools=[], retention=retention, policy=policy)
    assert len(sources) == 1 and all(text in sources[0] for text in texts)


@pytest.mark.parametrize("read_file", [False, True])
def test_legacy_summary_covers_every_compacted_step_of_a_long_run(tmp_path, read_file):
    class Provider:
        context_window_tokens = 32768
        max_output_tokens = 512

        def __init__(self):
            self.steps = 0
            self.sources = []

        async def complete(self, request):
            if request.purpose == "compaction":
                self.sources.append(request.items[0].content)
                return ModelResponse(content="summary")
            self.steps += 1
            if self.steps > 12:
                return ModelResponse(content="done")
            return ModelResponse(tool_calls=(
                ToolCall(id=f"call_{self.steps}", name="tool_report", arguments={"index": self.steps}),))

    @dagent.tool
    def report(index: int) -> str:
        return _long_result(index, rows=4000)

    provider = Provider()
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        agent = dagent.ToolAgent(
            profile="conversation", capabilities=[report, *(["tool.read_file"] if read_file else [])],
            max_steps=20, context=ContextPolicy(max_tool_result_tokens=4096),
        )
        result = asyncio.run(runner.run(agent, input="Collect every page."))
    assert result.status == "completed"
    compacted = [usage.compacted_items for usage in result.state.context_usage if usage.compacted_items]
    assert compacted and len(provider.sources) == len(compacted)
    for source, items in zip(provider.sources, compacted):
        # Each compacted step is one assistant call and its result; the fact
        # at the head of every one of them reaches the summary request.
        assert len(set(re.findall(r"FACT_\d+=", source))) == items // 2
    assert not result.conversation.summary.source_truncated
