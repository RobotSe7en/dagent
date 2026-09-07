"""Public failure snapshots survive required result-storage I/O errors."""

import asyncio
import base64

import pytest

import dagent
from dagent.capabilities.tools.registry import ToolOutput
from dagent.harness_runtime import result_storage
from dagent.providers import ChatResponse, MockProvider, ToolCall


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("stage", ["initial", "review", "after_review"])
@pytest.mark.parametrize("payload", ["binary", "json", "artifact"])
def test_required_storage_failure_returns_audit(tmp_path, monkeypatch, stream, stage, payload):
    calls = []

    @dagent.tool(risk="medium")
    def prepare():
        calls.append("prepare")
        return "prepared"

    @dagent.tool(risk="medium" if stage == "review" else "low")
    def produce(label: str):
        calls.append(label)
        return ToolOutput("produced", value=b"\xff" * 4096 if payload == "binary" else {"data": "x" * 4096})

    @dagent.tool
    def later():
        calls.append("later")
        return "unexpected"

    responses = []
    if stage == "after_review":
        responses.append(ChatResponse(tool_calls=[ToolCall(id="prepare", name="tool_prepare", arguments={})]))
    responses.extend([
        ChatResponse(tool_calls=[
            ToolCall(id="produce", name="tool_produce", arguments={"label": "once"}),
            ToolCall(id="later", name="tool_later", arguments={}),
        ]),
        ChatResponse(content="must not continue"),
    ])
    provider = MockProvider(responses)
    runner = dagent.Runner(workspace=tmp_path, provider=provider,
                           result_storage_policy=dagent.ResultStoragePolicy(max_inline_bytes=1024))
    if payload == "artifact":
        async def artifact_handler(invocation):
            calls.append(invocation.arguments["label"])
            return dagent.CapabilityResult.completed(invocation, "produced", artifacts=[{
                "type": "image", "mime_type": "image/png",
                "data": base64.b64encode(b"image bytes").decode(),
            }])

        produce = dagent.CapabilityBinding(produce.definition, artifact_handler)
    agent = dagent.ToolAgent(profile="conversation", capabilities=[prepare, produce, later],
                             review="fast" if stage == "initial" else "careful")
    # Even validation must not call the model after this failure.
    runner.enable_validation = True

    original = result_storage._write_reference

    def fail_required(*args, **kwargs):
        if kwargs.get("media_type") != "text/plain; charset=utf-8":
            raise OSError("disk full")
        return original(*args, **kwargs)

    monkeypatch.setattr(result_storage, "_write_reference", fail_required)

    async def exercise():
        if stage != "initial":
            first = await runner.run(agent, input="produce", run_id="audit_run")
            assert first.requires_review
            checkpoint = first.checkpoint
            decision = first.review.approve()
            operation = runner.resume_stream(decision, checkpoint=checkpoint) if stream else runner.resume(decision, checkpoint=checkpoint)
        else:
            operation = runner.stream(agent, input="produce", run_id="audit_run") if stream else runner.run(agent, input="produce", run_id="audit_run")
        if stream:
            events = [event async for event in operation]
            assert events[-1].type == "run.failed"
            assert sum(event.type == "run.failed" for event in events) == 1
            assert not any(event.type == "run.finished" for event in events)
            failed = events[-1]
            assert failed.run_id == "audit_run"
            assert failed.data.error_type == "ResultStorageError"
            assert "disk full" in failed.data.message
            restored = dagent.RunStreamEvent.model_validate(failed.model_dump(mode="json"))
            assert restored == failed
            return failed.data.result
        with pytest.raises(dagent.RunExecutionError) as raised:
            await operation
        assert isinstance(raised.value.__cause__, result_storage.ResultStorageError)
        assert isinstance(raised.value.__cause__.__cause__, OSError)
        return raised.value.result

    try:
        result = asyncio.run(exercise())
        assert result.status == "failed"
        assert result.kind == "tool"
        assert result.state.user_request == "produce"
        assert result.state.workspace_path
        assert result.checkpoint is None
        assert result.pending_review is None
        assert result.state.pending_tool_batch is None
        assert runner.run_checkpoint(result.run_id) is None
        assert runner.run_state(result.run_id).status == "failed"
        assert result.trace.root.status == "failed"
        assert result.trace.root.ended_at is not None
        executions = [node for node in result.trace.root.children if node.capability_execution is not None]
        failed_nodes = [node for node in executions if node.capability_execution.invocation.invocation_id == "produce"]
        assert len(failed_nodes) == 1
        node = failed_nodes[0]
        assert node.error.code == "ResultStorageError"
        assert node.status == "failed"
        assert node.ended_at is not None
        audit = node.capability_execution.result
        assert audit.status == "completed"
        assert audit.capability_id == "tool.produce"
        assert node.capability_execution.invocation.arguments == {"label": "once"}
        warning = audit.retention.storage_warnings[-1]
        assert warning.field == ("artifacts[0]" if payload == "artifact" else "value")
        assert warning.error_type == "OSError"
        assert warning.message == "disk full"
        if payload == "binary":
            assert audit.value is None
            assert audit.retention.unavailable_fields == ("value",)
        assert dagent.RunResult.model_validate(result.model_dump(mode="json")).status == "failed"
        expected_calls = ["prepare", "once"] if stage == "after_review" else ["once"]
        assert calls == expected_calls
        assert len(provider.requests) == (2 if stage == "after_review" else 1)
        assert result.usage.model_turns == len(provider.requests)
        assert result.usage.capability_calls == len(expected_calls)
        assert result.context_usage
    finally:
        runner.close()


def test_plain_stream_failure_has_no_snapshot(tmp_path):
    runner = dagent.Runner(workspace=tmp_path, provider=MockProvider())

    async def exercise():
        return [event async for event in runner.stream(dagent.ToolAgent(profile="conversation"))]

    try:
        failed = asyncio.run(exercise())[-1]
        assert failed.type == "run.failed"
        assert failed.data.error_type == "TypeError"
        assert failed.data.result is None
        from dagent.result import RunFailedData
        assert RunFailedData(message="old payload", error_type="TypeError").result is None
    finally:
        runner.close()


def test_direct_loop_without_review_guard_keeps_failed_call(tmp_path, monkeypatch):
    from dagent.harness_runtime.tool_agent import ToolResultStorageFailure

    @dagent.tool
    def binary():
        return ToolOutput("binary", value=b"\xff")

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="binary", name="tool_binary", arguments={})]),
        ChatResponse(content="unexpected"),
    ])
    runner = dagent.Runner(workspace=tmp_path, provider=provider, capabilities=[binary])

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(result_storage, "_write_reference", fail)
    try:
        with pytest.raises(ToolResultStorageFailure) as raised:
            asyncio.run(runner._runtime.tool_agent.run("binary", review_level=None))
        state = raised.value.outcome.state
        assert state.status == "failed"
        node = state.trace.root.children[-1]
        assert node.error.code == "ResultStorageError"
        assert node.capability_execution.result.status == "completed"
        assert node.capability_execution.result.retention.unavailable_fields == ("value",)
        assert len(provider.requests) == 1
    finally:
        runner.close()


@pytest.mark.parametrize("validation_retry", [0, 1, 2])
def test_failure_preserves_orchestration_audit(tmp_path, monkeypatch, validation_retry):
    from dagent.harness_runtime.runtime import HarnessRuntime

    @dagent.tool
    def binary():
        return ToolOutput("binary", value=b"\xff")

    @dagent.tool
    def prepare():
        return "prepared"

    responses = [ChatResponse(content="tool")]
    for attempt in range(validation_retry):
        responses.extend([
            ChatResponse(tool_calls=[ToolCall(id=f"prepare_{attempt}", name="tool_prepare", arguments={})]),
            ChatResponse(content=f"answer {attempt}"),
        ])
    responses.append(ChatResponse(tool_calls=[ToolCall(id="binary", name="tool_binary", arguments={})]))
    provider = MockProvider(responses)
    runner = dagent.Runner(workspace=tmp_path, provider=provider)
    runner.runtime.max_validation_retries = 2
    agent = dagent.AutoAgent(capabilities=[binary, prepare], skills=[])
    validations = []

    async def reject(self, outcome, request, *, on_event):
        validations.append(request)
        return False, "Try the binary tool", None, None

    if validation_retry:
        monkeypatch.setattr(HarnessRuntime, "_validate_loop_outcome", reject)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(result_storage, "_write_reference", fail)
    try:
        with pytest.raises(dagent.RunExecutionError) as raised:
            asyncio.run(runner.run(agent, input="produce"))
        result = raised.value.result
        assert result.new_items[0].content == "produce"
        assert result.new_items[1].scope == "router"
        assert result.usage.model_turns == len(responses)
        assert len(provider.requests) == len(responses)
        assert len(validations) == int(validation_retry)
        invocations = [node.capability_execution.invocation.invocation_id
                       for node in result.trace.root.children if node.capability_execution]
        assert invocations == [*(f"prepare_{attempt}" for attempt in range(validation_retry)), "binary"]
        assert result.checkpoint is None
    finally:
        runner.close()
