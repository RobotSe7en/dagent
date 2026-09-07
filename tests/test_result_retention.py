from __future__ import annotations

import asyncio
import json
import sys

import pytest

from dagent.capabilities.tools.file_tools import read_file, grep, list_files
from dagent.capabilities.tools.shell_tools import shell
from dagent.harness_runtime.context import ContextAssembler
from dagent.harness_runtime.result_storage import ResultStore
from dagent.schemas import (
    AssistantMessage,
    ContextPolicy,
    ConversationState,
    ToolCallItem,
    ToolResultMessage,
)
from dagent.schemas.conversation import ContentReference, InlineContent


@pytest.mark.parametrize("count,budget", [(5, 8192), (9, 16384)])
def test_every_large_result_has_a_recoverable_floor(tmp_path, count, budget):
    calls = tuple(ToolCallItem(id=str(i), name="tool_large") for i in range(count))
    originals = [f"result {i}\n" + "数据" * 12000 for i in range(count)]
    conversation = ConversationState(
        items=(
            AssistantMessage(tool_calls=calls),
            *(
                ToolResultMessage(
                    call_id=str(i),
                    name="tool_large",
                    status="completed",
                    content=InlineContent(text=text),
                )
                for i, text in enumerate(originals)
            ),
        )
    )
    store = ResultStore(tmp_path, ".runtime")
    assembler = ContextAssembler(context_window_tokens=131072)
    prepared = asyncio.run(
        assembler.prepare(
            system_message={"content": "test"},
            conversation=conversation,
            policy=ContextPolicy(max_total_tool_result_tokens=budget),
            result_store=store,
        )
    )
    results = [
        item
        for item in prepared.conversation.items
        if isinstance(item, ToolResultMessage)
    ]
    assert len(results) == count
    for item, original in zip(results, originals):
        assert isinstance(item.content, ContentReference)
        assert (tmp_path / item.content.path).read_text() == original
    for message in prepared.messages:
        if message["role"] == "tool":
            assert "status=completed" in message["content"]
            assert "[TRUNCATED]" in message["content"]
            assert "path=" in message["content"]
            assert assembler.token_counter.count_text(message["content"]) <= 2048
    assert prepared.usage.tool_result_tokens <= budget
    files = list((tmp_path / ".runtime/results").iterdir())
    asyncio.run(
        assembler.prepare(
            system_message={"content": "test"},
            conversation=prepared.conversation,
            policy=ContextPolicy(max_total_tool_result_tokens=budget),
            result_store=store,
        )
    )
    assert list((tmp_path / ".runtime/results").iterdir()) == files


def test_character_windows_recover_a_long_unicode_line(tmp_path):
    text = "你好🙂abcdef" * 30000
    path = tmp_path / "long.txt"
    path.write_text(text, encoding="utf-8")
    offset = 0
    parts = []
    while offset < len(text):
        output = read_file(path, offset_chars=offset, limit_chars=1024)
        parts.append(output.content)
        offset = output.retention["continuation"]["offset"]
    assert "".join(parts) == text


def test_query_pages_do_not_repeat_or_omit_entries(tmp_path):
    for i in range(7):
        (tmp_path / f"{i}.txt").write_text("match\n")
    first = list_files(tmp_path, limit=3)
    second = list_files(tmp_path, offset=3, limit=3)
    third = list_files(tmp_path, offset=6, limit=3)
    assert len(set(first.value + second.value + third.value)) == 7
    first = grep(tmp_path, "match", limit=3)
    second = grep(tmp_path, "match", offset=3, limit=3)
    assert first.retention["continuation"]["offset"] == 3
    assert "3.txt" in second.content and "0.txt" not in second.content


def test_shell_capture_limit_keeps_exit_status_and_tail(tmp_path):
    output = shell(
        f"\"{sys.executable}\" -c \"print('x'*10000); print('END')\"",
        cwd=tmp_path,
        _dagent_result_context={
            "workspace": str(tmp_path),
            "runtime_directory": ".runtime",
            "max_bytes": 1024,
        },
    )
    assert "END" in output.content
    assert output.retention["source_completeness"] == "partial"
    assert output.retention["retained_bytes"] <= 1024
    assert output.content_reference is not None
    assert (tmp_path / output.content_reference["path"]).stat().st_size < 1100


def test_storage_failure_is_explicit_and_preserves_original(tmp_path, monkeypatch):
    store = ResultStore(tmp_path, ".runtime")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save_text", fail)
    item = ToolResultMessage(
        call_id="a",
        name="tool_a",
        status="completed",
        content=InlineContent(text="data"),
    )
    with pytest.warns(RuntimeWarning, match="disk full"):
        saved = store.ensure(item)
    assert saved.status == "completed"
    assert saved.content == item.content
    assert saved.retention.storage_warnings[0].error_type == "OSError"


def test_line_window_projection_continues_at_exact_source_character(tmp_path):
    from dagent.schemas.retention import ResultRetention
    from dagent.harness_runtime.result_projection import project_result

    path = tmp_path / "windows.txt"
    source = "first\r\n" + "中文🙂" * 1000 + "\r\nlast\r\n"
    path.write_bytes(source.encode("utf-8"))
    output = read_file(path, offset=2, limit=1)
    item = ToolResultMessage(
        call_id="read",
        name="tool_read_file",
        status="completed",
        content=InlineContent(text=output.content),
        retention=ResultRetention.model_validate(output.retention),
    )
    counter = ContextAssembler().token_counter
    projected = project_result(item, 256, counter, read_available=True)
    cursor_line = next(
        line
        for line in projected.text.splitlines()
        if line.startswith("[Continue with read_file:")
    )
    args = json.loads(cursor_line[len("[Continue with read_file: ") : -1])
    body = projected.text.split(cursor_line + "\n", 1)[1]
    assert source[len("first\r\n") : args["offset_chars"]] == body
    assert (
        read_file(path, offset_chars=args["offset_chars"], limit_chars=32).content
        == source[args["offset_chars"] : args["offset_chars"] + 32]
    )


def test_repeated_compaction_keeps_original_result_index(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    assembler = ContextAssembler(context_window_tokens=8192)
    policy = ContextPolicy(max_tool_result_tokens=128, max_total_tool_result_tokens=128)
    items = []
    for i in range(4):
        items.extend(
            (
                AssistantMessage(
                    tool_calls=(
                        ToolCallItem(id=str(i), name="tool_a", arguments={"index": i}),
                    )
                ),
                ToolResultMessage(
                    call_id=str(i),
                    name="tool_a",
                    status="completed",
                    content=InlineContent(text=f"original {i}: " + "data " * 1000),
                ),
            )
        )
    prepared = asyncio.run(
        assembler.prepare(
            system_message={"content": "test"},
            conversation=ConversationState(items=tuple(items)),
            policy=policy,
            result_store=store,
        )
    )
    summary = prepared.conversation.summary
    assert summary and summary.result_manifest
    rows = [
        json.loads(line)
        for line in (tmp_path / summary.result_manifest.path).read_text().splitlines()
    ]
    results = [row for row in rows if row["type"] == "tool_result"]
    assert {row["call_id"] for row in results} == {"0", "1", "2"}
    for row in results:
        assert (
            (tmp_path / row["content"]["path"])
            .read_text()
            .startswith(f"original {row['call_id']}:")
        )
    assert any(
        row["type"] == "tool_calls" and row["calls"][0]["arguments"] == {"index": 0}
        for row in rows
    )


def test_storage_warning_event_roundtrips():
    from dagent.runner import _stream_event_from_runtime
    from dagent.result import RunStreamEvent

    event = _stream_event_from_runtime(
        {
            "type": "tool_result_storage_warning",
            "invocation_id": "call",
            "warning": {
                "field": "content",
                "error_type": "OSError",
                "message": "disk full",
            },
        }
    )
    assert event.type == "capability.result.storage_warning"
    assert RunStreamEvent.model_validate(event.model_dump(mode="json")) == event


def test_required_value_storage_error_keeps_successful_execution(tmp_path, monkeypatch):
    from dagent.harness_runtime import result_storage
    from dagent.schemas import CapabilityResult, ResultStoragePolicy

    result = CapabilityResult(
        invocation_id="call",
        capability_id="tool.binary",
        kind="tool",
        status="completed",
        value=b"\xff" * 1024,
    )

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(result_storage, "_write_reference", fail)
    with pytest.raises(result_storage.ResultStorageError) as raised:
        result_storage.normalize_capability_result(
            result,
            workspace_path=tmp_path,
            runtime_directory=".runtime",
            policy=ResultStoragePolicy(),
        )
    assert raised.value.result.status == "completed"
    assert raised.value.result.value == result.value


def test_saved_results_do_not_expand_read_boundaries(tmp_path):
    from dagent.schemas.common import Boundary

    store = ResultStore(
        tmp_path, ".runtime", read_boundary=Boundary(allowed_paths=["allowed"])
    )
    conversation = ConversationState(
        items=(
            AssistantMessage(tool_calls=(ToolCallItem(id="call", name="tool_data"),)),
            ToolResultMessage(
                call_id="call",
                name="tool_data",
                status="completed",
                content=InlineContent(text="data " * 5000),
            ),
        )
    )
    prepared = asyncio.run(
        ContextAssembler().prepare(
            system_message={"content": "test"},
            conversation=conversation,
            policy=ContextPolicy(),
            result_store=store,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "tool_read_file",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )
    )
    assert "RECOVERY_UNAVAILABLE" in prepared.messages[-1]["content"]
    assert prepared.usage.unrecoverable_tool_results == 1
    item = prepared.conversation.items[-1]
    assert isinstance(item.content, ContentReference)
    assert (tmp_path / item.content.path).is_file()


def test_storage_warning_does_not_fail_a_streamed_run(tmp_path, monkeypatch):
    import dagent
    from dagent.providers import ChatResponse, MockProvider, ToolCall

    @dagent.tool
    def data() -> str:
        return "payload " * 5000

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ResultStore, "save_text", fail)

    async def collect():
        runner = dagent.Runner(
            workspace=tmp_path,
            provider=MockProvider(
                [
                    ChatResponse(
                        tool_calls=[ToolCall(id="call", name="tool_data", arguments={})]
                    ),
                    ChatResponse(content="done"),
                ]
            ),
            capabilities=[data],
        )
        try:
            return [
                event
                async for event in runner.stream(
                    dagent.ToolAgent(
                        profile="conversation", capabilities=["tool.data"]
                    ),
                    input="test",
                )
            ]
        finally:
            runner.close()

    with pytest.warns(RuntimeWarning, match="disk full"):
        events = asyncio.run(collect())
    assert any(event.type == "capability.result.storage_warning" for event in events)
    assert not any(event.type == "capability.call.failed" for event in events)
    assert events[-1].type == "run.finished"


def test_shell_memory_does_not_scale_with_drained_output(tmp_path):
    import tracemalloc

    tracemalloc.start()
    try:
        output = shell(
            f"\"{sys.executable}\" -c \"import sys; [sys.stdout.write('x'*65536) for _ in range(512)]; print('END')\"",
            cwd=tmp_path,
            _dagent_result_context={
                "workspace": str(tmp_path),
                "runtime_directory": ".runtime",
                "max_bytes": 1024,
            },
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert output.retention["received_bytes"] >= 32 * 1024 * 1024
    assert output.retention["retained_bytes"] <= 1024
    assert "END" in output.content
    assert peak < 8 * 1024 * 1024


def test_python_search_does_not_follow_file_symlinks(tmp_path, monkeypatch):
    from dagent.capabilities.tools import file_tools

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret match")
    try:
        (root / "link.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(file_tools, "_ripgrep_executable", lambda: None)
    assert grep(root, "secret").content == ""


def test_required_binary_failure_blocks_dag_dependents_and_checkpoint(
    tmp_path, monkeypatch
):
    import dagent
    from dagent.harness_runtime import result_storage
    from dagent.providers import MockProvider

    calls = []

    @dagent.tool
    def binary():
        from dagent.capabilities.tools.registry import ToolOutput

        return ToolOutput("binary payload", value=b"\xff" * 4096)

    @dagent.tool
    def consume(data):
        calls.append(data)
        return "unexpected"

    graph = dagent.Dag("storage_failure")
    producer = dagent.Node("produce", target=binary)
    consumer = dagent.Node("consume", target=consume, inputs={"data": producer.output})
    graph.add_node(producer)
    graph.add_node(consumer)
    graph.add_edge(producer, consumer)
    original = result_storage._write_reference

    def fail_binary(*args, **kwargs):
        if kwargs.get("media_type") == "application/octet-stream":
            raise OSError("disk full")
        return original(*args, **kwargs)

    monkeypatch.setattr(result_storage, "_write_reference", fail_binary)
    runner = dagent.Runner(workspace=tmp_path, provider=MockProvider())
    try:
        result = asyncio.run(runner.run(graph))
    finally:
        runner.close()
    assert result.status == "failed"
    assert calls == []
    assert result.checkpoint is None
    recorded = (
        result.trace.dag_node_traces()["produce"]
        .children[0]
        .capability_execution.result
    )
    assert recorded.status == "completed"
    assert recorded.retention.unavailable_fields == ("value",)
    assert result.model_dump(mode="json")
