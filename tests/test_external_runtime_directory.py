"""Absolute private storage preserves logical references and run isolation."""

import asyncio
from pathlib import Path
import shutil

import pytest

import dagent
from dagent.capabilities.sandbox import SandboxSession, build_skeleton
from dagent.capabilities.tools.runtime_paths import RuntimePaths, runtime_storage_context
from dagent.harness_runtime.conversation_resources import ConversationResourceStore
from dagent.harness_runtime.result_storage import ResultStore
from dagent.providers import ChatResponse, MockProvider, ToolCall


def run(awaitable):
    return asyncio.run(awaitable)


def results(result):
    return [item for item in result.conversation.items if isinstance(item, dagent.ToolResultMessage)]


@pytest.mark.parametrize("as_string", [False, True])
def test_absolute_directory_is_explicit_and_created_only_when_needed(tmp_path, as_string):
    private = tmp_path / "private"
    runner = dagent.Runner(
        workspace=tmp_path / "workspace", runtime_directory=str(private) if as_string else private,
        provider=MockProvider([ChatResponse(content="hello")]),
    )
    try:
        assert runner.runtime_directory == str(private)
        assert not private.exists()
        result = run(runner.run(dagent.ToolAgent(profile="conversation"), input="hello"))
        assert result.output_text == "hello"
        assert not private.exists()
        assert not (Path(result.workspace_path) / ".runtime").exists()
        derived = runner.derive(workspace=tmp_path / "other")
        try:
            assert derived.runtime_directory == str(private)
        finally:
            derived.close()
    finally:
        runner.close()


def test_external_results_restore_after_source_run_is_removed(tmp_path):
    body = "row: retained result\n" * 300

    @dagent.tool
    def report() -> str:
        return body

    private = tmp_path / "private"
    source_workspace = tmp_path / "first"
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="report", name="tool_report", arguments={})]),
        ChatResponse(content="saved"),
    ])
    runner = dagent.Runner(
        workspace=source_workspace, runtime_directory=private, provider=provider,
        capabilities=[report], result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024),
    )
    try:
        first = run(runner.run(dagent.ToolAgent(profile="conversation", capabilities=["tool.report"]), input="report", workspace_path=source_workspace))
        reference = results(first)[0].content
        assert isinstance(reference, dagent.ContentReference)
        assert reference.path.startswith(".runtime/results/")
        saved = runner.resolve_runtime_path(reference.path, workspace_path=source_workspace)
        assert saved.read_text() == body
        assert saved.is_relative_to(private)
        assert not (source_workspace / ".runtime").exists()
        assert first.plan.runtime_directory == str(private)
        conversation = dagent.ConversationState.model_validate_json(first.conversation.model_dump_json())
    finally:
        runner.close()
    shutil.rmtree(saved.parent.parent)
    shutil.rmtree(source_workspace)

    next_workspace = tmp_path / "second"
    runner = dagent.Runner(
        workspace=next_workspace, runtime_directory=private,
        provider=MockProvider([ChatResponse(content="continued")]),
    )
    try:
        second = run(runner.run(dagent.ToolAgent(profile="conversation"), input="continue", conversation=conversation, workspace_path=next_workspace))
        restored = results(second)[0].content
        assert runner.resolve_runtime_path(restored.path, workspace_path=next_workspace).read_text() == body
        read = run(runner.test_capability("tool.read_file", {"path": restored.path, "offset_chars": 0, "limit_chars": 20}))
        assert read.status == "completed"
        assert read.content == body[:20]
        assert read.retention.continuation.path == restored.path
        denied = run(runner.test_capability(
            "tool.read_file", {"path": restored.path},
            boundary=dagent.Boundary(allowed_paths=["other"]),
        ))
        assert denied.stop_reason == "BoundaryViolation"
        searched = run(runner.test_capability("tool.grep", {"path": restored.path, "pattern": "retained", "limit": 2}))
        assert searched.status == "completed"
        assert str(private) not in searched.content
        listed = run(runner.test_capability("tool.list_files", {"path": ".runtime/history"}))
        assert listed.status == "completed"
        assert all(str(private) not in entry for entry in listed.value)
        blocked = run(runner.test_capability("tool.write_file", {"path": restored.path, "content": "replace"}))
        assert blocked.stop_reason == "HardBoundaryViolation"
        assert not (next_workspace / ".runtime").exists()
    finally:
        runner.close()


def test_shell_output_is_external_and_workspaces_are_isolated(tmp_path):
    private = tmp_path / "private"
    runner = dagent.Runner(
        workspace=tmp_path / "workspace", runtime_directory=private, provider=MockProvider([]),
    )
    runner.workspace.mkdir()
    try:
        output = run(runner.test_capability("tool.shell", {"command": "printf 'hello\\n'"}))
        assert output.status == "completed"
        reference = output.content_reference
        saved = runner.resolve_runtime_path(reference.path, workspace_path=runner.workspace)
        assert "[stdout]\nhello\n" in saved.read_text()
        assert saved.is_relative_to(private)
        assert not (runner.workspace / ".runtime").exists()
        other = RuntimePaths(tmp_path / "other", str(private)).resolve(reference.path)
        assert other != saved
        assert not other.exists()
    finally:
        runner.close()


def test_static_dag_passes_externalized_values_to_dependents(tmp_path):
    body = {"rows": list(range(1000))}

    @dagent.tool
    def produce() -> dict:
        return body

    @dagent.tool
    def consume(data: dict) -> int:
        assert data == body
        return len(data["rows"])

    graph = dagent.Dag("external_values")
    source = dagent.Node("source", target=produce)
    target = dagent.Node("target", target=consume, inputs={"data": source.output})
    graph.add_node(source)
    graph.add_node(target)
    graph.add_edge(source, target)
    runner = dagent.Runner(
        workspace=tmp_path / "workspace", runtime_directory=tmp_path / "private", provider=MockProvider([]),
        result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024),
    )
    try:
        result = run(runner.run(graph))
        assert result.status == "completed"
        assert result.trace.root.children[0].value_reference is not None
        assert not (Path(result.workspace_path) / ".runtime").exists()
    finally:
        runner.close()


def test_review_resume_uses_frozen_absolute_directory(tmp_path):
    @dagent.tool(risk="medium")
    def report() -> str:
        return "saved evidence\n" * 300

    private = tmp_path / "private"
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="report", name="tool_report", arguments={})]),
        ChatResponse(content="done"),
    ])
    runner = dagent.Runner(
        workspace=tmp_path / "workspace", runtime_directory=private, provider=provider,
        result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024),
    )
    try:
        first = run(runner.run(dagent.ToolAgent(profile="conversation", capabilities=[report], review="careful"), input="report"))
        checkpoint = dagent.RunCheckpoint.model_validate_json(first.checkpoint.model_dump_json())
    finally:
        runner.close()
    other_private = tmp_path / "other-private"
    runner = dagent.Runner(
        workspace=tmp_path / "workspace", runtime_directory=other_private, provider=provider, capabilities=[report],
    )
    try:
        resumed = run(runner.resume(first.review.approve(), checkpoint=checkpoint))
        assert resumed.status == "completed"
        assert resumed.plan.runtime_directory == str(private)
        reference = results(resumed)[0].content
        saved = runner.resolve_runtime_path(
            reference.path, workspace_path=resumed.workspace_path,
            runtime_directory=resumed.plan.runtime_directory,
        )
        assert saved.read_text() == "saved evidence\n" * 300
        assert not other_private.exists()
        assert not (Path(resumed.workspace_path) / ".runtime").exists()
    finally:
        runner.close()


def test_external_snapshots_keep_integrity_and_reject_symlink_escape(tmp_path):
    workspace = tmp_path / "workspace"
    store = ResultStore(workspace, str(tmp_path / "private"))
    ref = store.snapshot_text("original")
    assert store.read_reference(ref) == b"original"
    saved = store.paths.resolve(ref.path)
    saved.write_text("modified")
    with pytest.raises(OSError, match="mismatch"):
        store.read_reference(ref)
    saved.unlink()
    outside = tmp_path / "outside"
    outside.write_text("original")
    saved.symlink_to(outside)
    with pytest.raises(ValueError):
        store.paths.resolve(ref.path)


def test_relative_storage_keeps_workspace_internal_symlinks_supported(tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    for name in ("results", "history", "conversations"):
        target = tmp_path / f"saved-{name}"
        target.mkdir()
        (runtime / name).symlink_to(target, target_is_directory=True)
    store = ResultStore(tmp_path, ".runtime")
    text = store.save_text("result", "saved")
    snapshot = store.snapshot_text("snapshot")
    assert (tmp_path / text.path).read_text() == "saved"
    assert (tmp_path / snapshot.path).read_text() == "snapshot"
    backup = ConversationResourceStore(tmp_path, ".runtime")
    assert backup.root == tmp_path / "saved-conversations"


def test_sandbox_mounts_run_resources_and_worker_uses_external_directory(tmp_path):
    from tests.test_sandbox import _run_worker

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private = tmp_path / "private"
    session = SandboxSession(dagent.DockerSandboxConfig(), workspace_root=workspace, runtime_directory=str(private))
    skeleton = build_skeleton(tmp_path / "skeleton")
    volumes = session._volumes(skeleton)
    assert volumes[str(private / "runs")]["mode"] == "rw"
    assert str(private / "conversations") not in volumes
    output = _run_worker(skeleton, "shell", {
        "command": "printf sandbox", "cwd": str(workspace),
        "_dagent_result_context": {"workspace": str(workspace), "runtime_directory": str(private), "max_bytes": 4096},
    })
    assert output["ok"]
    ref = dagent.ContentReference.model_validate(output["content_reference"])
    assert "sandbox" in RuntimePaths(workspace, str(private)).resolve(ref.path).read_text()
    assert not (workspace / ".runtime").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["chunked", "cache_aware"])
async def test_compaction_records_and_frozen_displays_restore_from_external_store(tmp_path, strategy):
    from dagent.harness_runtime.compaction import CompactionRecordV1, HistoryCompactor
    from dagent.harness_runtime.context import ContextAssembler
    from tests.test_compaction_strategy import summary_response
    from tests.test_frozen_results import _conversation, _freeze

    private = tmp_path / "private"
    workspace = tmp_path / "workspace"
    store = ResultStore(workspace, str(private))
    source = "Evidence must be retained.\n" * 500
    frozen = _freeze(_conversation(source), store, cap=128)
    display = frozen.items[1].model_display
    assert store.read_reference(display.recovery).decode() == source
    assert "TRUNCATED" in display.text

    async def summarize(request):
        return summary_response(request)

    with runtime_storage_context(str(private)):
        compactor = HistoryCompactor(
            policy=dagent.ContextPolicy(compaction_strategy=strategy),
            assembler=ContextAssembler(context_window_tokens=4096),
            result_store=store, system_message={"content": "Keep the evidence."},
            chat=summarize, read_available=True,
        )
        summary = await compactor.compact(None, frozen.items, 256)
        conversation = frozen.model_copy(update={"summary": summary})
        backup = ConversationResourceStore(tmp_path / "runner", str(private))
        backup.persist(conversation, workspace_path=workspace)
        shutil.rmtree(store.paths.root)
        destination = tmp_path / "next"
        restored = backup.materialize(conversation, workspace_path=destination)
        new_store = ResultStore(destination, str(private))
        assert new_store.read_reference(restored.items[1].model_display.recovery).decode() == source
        record = CompactionRecordV1.model_validate_json(new_store.read_reference(restored.summary.compaction_record))
        assert record.status == "complete"
        assert new_store.read_reference(record.source)
        assert not (destination / ".runtime").exists()


@pytest.mark.asyncio
async def test_concurrent_runners_keep_external_storage_contexts_separate(tmp_path):
    @dagent.tool
    async def report() -> str:
        await asyncio.sleep(0)
        return "report data\n" * 300

    runners = [dagent.Runner(
        workspace=tmp_path / f"workspace-{index}", runtime_directory=tmp_path / f"private-{index}",
        provider=MockProvider([
            ChatResponse(tool_calls=[ToolCall(id="report", name="tool_report", arguments={})]),
            ChatResponse(content="done"),
        ]), capabilities=[report], result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024),
    ) for index in range(2)]
    try:
        completed = await asyncio.gather(*(
            runner.run(dagent.ToolAgent(profile="conversation", capabilities=["tool.report"]), input="report")
            for runner in runners
        ))
        for runner, result in zip(runners, completed):
            saved = runner.resolve_runtime_path(results(result)[0].content.path, workspace_path=result.workspace_path)
            assert saved.is_relative_to(runner.runtime_directory)
            assert saved.read_text() == "report data\n" * 300
            assert not (Path(result.workspace_path) / ".runtime").exists()
    finally:
        for runner in runners:
            runner.close()
