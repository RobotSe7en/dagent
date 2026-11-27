"""Images survive tool, compaction, persistence and review boundaries."""

import base64
import hashlib
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

import dagent
from dagent.capabilities.images import ImageInputError, image_policy_context
from dagent.capabilities.mcp.handlers import make_mcp_tool_handler
from dagent.harness_runtime.images import save_image, project_images
from dagent.harness_runtime.result_storage import ResultStore
from dagent.harness_runtime.context import ContextAssembler
from dagent.harness_runtime.compaction import HistoryCompactor
from dagent.providers import MockProvider, ChatResponse, ToolCall
from dagent.providers.model_io import ModelRequest, ModelUserInput, ModelTokenCount, image_content, text_only_request
from tests.test_compaction_strategy import summary_response
from tests.test_image_inputs import picture, agent
from tests.planner_helpers import capability_plan_response


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_mcp_images_require_opt_in_and_reuse_artifact_storage(tmp_path, enabled):
    class Manager:
        async def call_tool(self, *args):
            return SimpleNamespace(isError=False, content=[
                {"type": "text", "text": "capture"},
                {"type": "image", "mimeType": "image/png", "data": base64.b64encode(picture()).decode()},
            ])

    handler = make_mcp_tool_handler(Manager(), server_name="browser", tool_name="capture",
                                    image_results_as_input=enabled)
    binding = dagent.CapabilityBinding(dagent.CapabilityDefinition(id="mcp.browser.capture", kind="mcp"), handler)
    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="shot", name="mcp_browser_capture", arguments={})]),
                             ChatResponse(content="seen")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        result = await runner.run(agent(capabilities=[binding]), input="capture")
    tool_result = result.conversation.items[-2]
    assert len(tool_result.images) == int(enabled)
    assert result.conversation.schema_version == (5 if enabled else 4)
    if enabled:
        image = tool_result.images[0]
        assert sum(reference.path == image.reference.path for reference in tool_result.artifacts) == 1
        assert list((Path(result.state.workspace_path) / ".runtime/images").iterdir())
        assert provider.requests[-1]["messages"][-1]["content"][-1]["type"] == "image_url"


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["%%%", base64.b64encode(b"not an image").decode()])
async def test_invalid_mcp_images_return_failed_tool_reply_and_keep_other_artifacts(tmp_path, data):
    class Manager:
        async def call_tool(self, *args):
            return SimpleNamespace(isError=False, content=[
                {"type": "image", "mimeType": "image/png", "data": data},
                {"type": "audio", "mimeType": "audio/wav", "data": base64.b64encode(b"audio").decode()},
            ])

    handler = make_mcp_tool_handler(Manager(), server_name="browser", tool_name="capture",
                                    image_results_as_input=True)
    binding = dagent.CapabilityBinding(dagent.CapabilityDefinition(id="mcp.browser.capture", kind="mcp"), handler)
    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="shot", name="mcp_browser_capture", arguments={})]),
                             ChatResponse(content="The screenshot is unavailable.")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        result = await runner.run(agent(capabilities=[binding]), input="capture")

    tool_result = result.conversation.items[-2]
    assert tool_result.status == "failed"
    assert not tool_result.images
    assert "[TOOL_ERROR]" in provider.requests[-1]["messages"][-1]["content"]
    assert len(tool_result.artifacts) == 1
    assert (Path(result.workspace_path) / tool_result.artifacts[0].path).read_bytes() == b"audio"
    audit = next(node.capability_execution.result for node in result.trace.root.children
                 if node.capability_execution is not None)
    assert audit.status == "failed" and audit.stop_reason == "ImageInputError"
    assert all("data" not in artifact for artifact in audit.artifacts)


@pytest.mark.asyncio
async def test_review_restores_frozen_image_policy_and_snapshots(tmp_path):
    @dagent.tool(risk="medium")
    def approve_action() -> str:
        return "approved"

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="action", name="tool_approve_action", arguments={})]),
                             ChatResponse(content="finished")])
    frozen_policy = dagent.ImageInputPolicy(estimated_tokens_per_image=321)
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, image_input_policy=frozen_policy)) as runner:
        result = await runner.run(agent(capabilities=[approve_action], review="careful"), input="look and act",
                                  input_uploads=[dagent.ArtifactUpload("a.png", picture(), model_input="image")])
        assert result.requires_review
        saved = dagent.RunCheckpoint.model_validate_json(result.checkpoint.model_dump_json())
        decision = result.review.approve()
    (Path(saved.state.workspace_path) / "uploads/a.png").unlink()
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, capabilities=[approve_action],
                              image_input_policy=dagent.ImageInputPolicy(estimated_tokens_per_image=999))) as runner:
        resumed = await runner.resume(decision, checkpoint=saved)
    assert resumed.output_text == "finished"
    assert resumed.context_usage[-1].estimated_image_tokens == 321
    assert resumed.plan.image_input_policy == frozen_policy
    dagent.RunResult.model_validate(resumed.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_missing_snapshot_returns_failed_audit_without_model_call(tmp_path):
    provider = MockProvider([ChatResponse(content="first")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        result = await runner.run(agent(), input="view", input_uploads=[
            dagent.ArtifactUpload("a", picture(), model_input="image")])
        image = result.conversation.items[0].images[0]
        (Path(result.state.workspace_path) / image.reference.path).unlink()
        with pytest.raises(ImageInputError) as failure:
            await runner.run(agent(), input="again", conversation=result.conversation,
                             workspace_path=result.state.workspace_path)
    assert failure.value.result.status == "failed"
    assert failure.value.result.conversation.items[0].images == (image,)
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_legacy_compaction_sees_image_and_keeps_recovery_reference(tmp_path):
    image = save_image(dagent.ImageData.from_bytes(picture()), tmp_path)
    history = dagent.ConversationState(schema_version=5, items=(
        dagent.UserMessage(id="image", content="Analyze this. " * 500, images=(image,)),
        dagent.AssistantMessage(content="It contains a red square. " * 300),
    ))
    provider = MockProvider([ChatResponse(content="The earlier image was red; inspect the saved snapshot for detail."),
                             ChatResponse(content="continued")])
    provider.configured_context_window_tokens = 4096
    provider.max_output_tokens = 512
    with closing(dagent.Runner(provider=provider, workspace=tmp_path,
                              image_input_policy=dagent.ImageInputPolicy(estimated_tokens_per_image=64))) as runner:
        result = await runner.run(agent(context=dagent.ContextPolicy(compaction_trigger_ratio=.4,
                                          summary_max_tokens=128)), input="continue", conversation=history,
                                  workspace_path=tmp_path)
    assert result.conversation.summary.images == (image,)
    compaction_input = provider.requests[0]["messages"][-1]["content"]
    assert compaction_input[-1]["type"] == "image_url"
    assert image.reference.path in compaction_input[0]["text"]
    assert result.context_usage[-1].image_count == 0
    dagent.ConversationState.model_validate_json(result.conversation.model_dump_json())


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["chunked", "cache_aware"])
async def test_new_compaction_strategies_preserve_visual_sources(tmp_path, strategy):
    image = save_image(dagent.ImageData.from_bytes(picture()), tmp_path)
    items = (dagent.UserMessage(id="original", content="describe image", images=(image,)),)
    requests = []

    async def count(request, field):
        return ModelTokenCount(count=512, exact=True, includes_image_tokens=True)

    async def summarize(request):
        requests.append(request)
        return summary_response(text_only_request(request))

    source = ModelRequest(instructions="original", items=(ModelUserInput(
        source_id="original", content=image_content("describe image", project_images((image,), tmp_path))),))
    compactor = HistoryCompactor(
        policy=dagent.ContextPolicy(compaction_strategy=strategy),
        assembler=ContextAssembler(context_window_tokens=32768, request_token_counter=count),
        result_store=ResultStore(tmp_path, ".runtime"), system_message={"content": "original"},
        source_request=source, chat=summarize,
    )
    summary = await compactor.compact(None, items, 256)
    assert any(getattr(item, "images", ()) for request in requests for item in request.items)
    assert summary.images == (image,)
    archive = compactor.result_store.read_reference(summary.compaction_record)
    assert archive and summary.compaction_record.sha256


@pytest.mark.asyncio
async def test_strict_budget_does_not_trust_text_only_counter(tmp_path):
    image = save_image(dagent.ImageData.from_bytes(picture()), tmp_path)

    async def count(request, field):
        return ModelTokenCount(count=1, exact=True)

    compactor = HistoryCompactor(
        policy=dagent.ContextPolicy(compaction_strategy="chunked"),
        assembler=ContextAssembler(context_window_tokens=32768, request_token_counter=count),
        result_store=ResultStore(tmp_path, ".runtime"), system_message={"content": "original"},
        chat=lambda request: pytest.fail("Unverified visual input must not be dispatched"),
    )
    with pytest.raises(ImageInputError, match="counter"):
        await compactor.compact(None, (dagent.UserMessage(content="describe", images=(image,)),), 256)


@pytest.mark.asyncio
async def test_external_runtime_storage_and_sandbox_transport(tmp_path):
    from dagent.capabilities.tools.runtime_paths import runtime_storage_context
    from dagent.capabilities.tools.sandbox_worker import run_request

    source = tmp_path / "source.png"
    source.write_bytes(picture())
    transported = run_request({"tool": "read_image", "args": {"path": str(source)}})
    assert transported["ok"]
    assert base64.b64decode(transported["images"][0]["data"]) == picture()
    physical = tmp_path / "external"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with runtime_storage_context(str(physical)):
        image = save_image(dagent.ImageData.from_bytes(picture()), workspace)
        assert not (workspace / image.reference.path).exists()
        assert project_images((image,), workspace)[0].width == 12


@pytest.mark.asyncio
@pytest.mark.parametrize("auto", [False, True])
async def test_persistence_delivers_snapshot_before_model_uses_it(tmp_path, auto):
    from dagent.schemas.conversation_changes import ConversationChangeBatch

    snapshots = {}

    class Provider(MockProvider):
        async def chat(self, *args, **kwargs):
            assert any(reference.startswith(".runtime/images/") for reference in snapshots)
            return await super().chat(*args, **kwargs)

    batches = []
    provider = Provider(([ChatResponse(content="tool")] if auto else []) + [ChatResponse(content="seen")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        target = dagent.AutoAgent(capabilities=[]) if auto else agent()
        async for event in runner.stream(target, input="view", persistence=dagent.StreamPersistenceOptions(),
                                        input_uploads=[dagent.ArtifactUpload("a.png", picture(), model_input="image")]):
            if event.type != "conversation.changed":
                continue
            batch = ConversationChangeBatch.model_validate_json(event.data.model_dump_json())
            for resource in batch.resources:
                data = runner.resolve_runtime_path(resource.reference.path, workspace_path=resource.workspace_path,
                                                   runtime_directory=resource.runtime_directory).read_bytes()
                if resource.reference.sha256:
                    assert hashlib.sha256(data).hexdigest() == resource.reference.sha256
                snapshots[resource.reference.path] = data
            batches.append(batch)
            await runner.acknowledge_changes(batch.run_id, batch.batch_id)
    assert batches[-1].status == "completed"
    assert batches[-1].conversation_schema_version == 5
    user = next(record.item for batch in batches for record in batch.messages if isinstance(record.item, dagent.UserMessage))
    restored = dagent.ConversationState(schema_version=5, items=(user,))
    assert restored.items[0].images[0].reference.path in snapshots


@pytest.mark.asyncio
async def test_static_dag_agent_reads_only_declared_artifact(tmp_path):
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="read", name="tool_read_image", arguments={"path": "inputs/a.png"})]),
        ChatResponse(content="seen"),
    ])
    graph = dagent.Dag("images")
    artifact = graph.artifact("image", "inputs/a.png")
    viewer = agent(capabilities=["tool.read_image"])
    graph.add_node(dagent.Node("describe", target=viewer, inputs={"prompt": "read inputs/a.png"},
                               artifact_inputs=[artifact]))
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        runner.add_image_tools()
        result = await runner.run(graph, artifact_uploads={"image": [dagent.ArtifactUpload("a.png", picture())]})
        with pytest.raises(ValueError, match="model_input"):
            await runner.run(graph, artifact_uploads={"image": [
                dagent.ArtifactUpload("a.png", picture(), model_input="image")]})
    assert result.status == "completed"
    assert provider.requests[-1]["messages"][-1]["content"][-1]["type"] == "image_url"
    dagent.RunResult.model_validate(result.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_visual_compaction_failure_keeps_source_conversation(tmp_path):
    image = save_image(dagent.ImageData.from_bytes(picture()), tmp_path)
    conversation = dagent.ConversationState(schema_version=5, items=(
        dagent.UserMessage(content="history " * 3000, images=(image,)),
        dagent.AssistantMessage(content="history " * 2000),
        dagent.UserMessage(content="continue"),
    ))
    async def fail(*args):
        raise RuntimeError("summarizer offline")
    with image_policy_context(dagent.ImageInputPolicy(estimated_tokens_per_image=64)):
        with pytest.raises(ImageInputError, match="summarizer offline"):
            await ContextAssembler(context_window_tokens=4096, max_output_tokens=512).prepare(
                conversation=conversation, system_message={"content": "help"}, policy=dagent.ContextPolicy(),
                result_store=ResultStore(tmp_path, ".runtime"), compact=fail,
            )
    assert conversation.items[0].images == (image,) and conversation.summary is None


@pytest.mark.asyncio
async def test_archived_images_survive_later_text_only_fallback(tmp_path):
    image = save_image(dagent.ImageData.from_bytes(picture()), tmp_path)
    previous = dagent.ContextSummary(content="Earlier image was red.", source_item_count=2, images=(image,))
    conversation = dagent.ConversationState(schema_version=5, summary=previous, items=(
        dagent.UserMessage(content="text-only history"), dagent.AssistantMessage(content="confirmed"),
        dagent.UserMessage(content="continue"),
    ))

    async def fail(*args):
        raise RuntimeError("summarizer offline")

    updated, method, _ = await ContextAssembler()._compact_slice(
        conversation, start=0, end=2, policy=dagent.ContextPolicy(), compact=fail,
        result_store=ResultStore(tmp_path, ".runtime"),
    )
    assert method == "deterministic_fallback"
    assert updated.summary.images == (image,)
    assert updated.schema_version == 5


@pytest.mark.asyncio
async def test_persistence_rejection_prevents_any_visual_generation(tmp_path):
    provider = MockProvider([ChatResponse(content="must not run")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        events = []
        async for event in runner.stream(agent(), input="view", persistence=dagent.StreamPersistenceOptions(),
                                        input_uploads=[dagent.ArtifactUpload("a", picture(), model_input="image")]):
            events.append(event)
            if event.type == "conversation.changed":
                await runner.reject_changes(event.run_id, event.data.batch_id, message="cannot save snapshots")
    assert not provider.requests
    assert events[-1].type == "run.failed"


@pytest.mark.asyncio
async def test_image_reads_obey_symlink_boundaries(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(picture())
    (workspace / "escape.png").symlink_to(outside)
    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="read", name="tool_read_image",
                                                               arguments={"path": "escape.png"})])])
    with closing(dagent.Runner(provider=provider, workspace=workspace)) as runner:
        runner.add_image_tools()
        result = await runner.run(agent(capabilities=["tool.read_image"], max_steps=1, review="careful"), input="read",
                                  workspace_path=workspace)
    assert result.requires_review or result.status == "failed"
    assert not any(getattr(item, "images", ()) for item in result.conversation.items)
    assert not (workspace / ".runtime/images").exists()


@pytest.mark.asyncio
async def test_dynamic_review_missing_image_keeps_completed_work_in_failed_audit(tmp_path):
    calls = []

    @dagent.tool
    def approve_action() -> str:
        calls.append("executed")
        return "approved work completed"

    provider = MockProvider([ChatResponse(content=capability_plan_response("tool.approve_action", {}))])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        result = await runner.run(dagent.DagAgent(capabilities=[approve_action], review="careful"),
            input="look and act", input_uploads=[dagent.ArtifactUpload("a.png", picture(), model_input="image")])
        assert result.requires_review
        image = result.conversation.items[0].images[0]
        (Path(result.workspace_path) / image.reference.path).unlink()

        with pytest.raises(ImageInputError) as failure:
            await runner.resume(result.review.approve(), checkpoint=result.checkpoint)

    failed = failure.value.result
    assert failed is not None
    assert failed.kind == "dynamic_dag" and failed.status == "failed"
    assert failed.trace.root.error.code == "ImageInputError"
    completed = failed.trace.dag_node_traces()["node_1"]
    assert completed.status == "completed"
    audit = next(node.capability_execution.result for node in completed.children
                 if node.capability_execution is not None)
    assert audit.content == "approved work completed"
    assert failed.conversation.items[0].images == (image,)
    assert calls == ["executed"]
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_dynamic_planner_missing_image_preserves_failure_state(tmp_path):
    image = save_image(dagent.ImageData.from_bytes(picture()), tmp_path)
    (tmp_path / image.reference.path).unlink()
    conversation = dagent.ConversationState(schema_version=5, items=(
        dagent.UserMessage(content="look", images=(image,)), dagent.AssistantMessage(content="done"),
    ))
    provider = MockProvider()
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        with pytest.raises(ImageInputError) as failure:
            await runner.run(dagent.DagAgent(capabilities=[]), input="continue", conversation=conversation,
                             workspace_path=tmp_path)
    assert failure.value.result.kind == "dynamic_dag"
    assert failure.value.result.status == "failed"
    assert failure.value.result.conversation.items[0].images == (image,)
    assert not provider.requests


@pytest.mark.asyncio
async def test_cancelling_visual_stream_preserves_confirmed_resources(tmp_path):
    import asyncio
    from dagent.providers import ChatStreamEvent

    class BlockingProvider(MockProvider):
        async def stream_chat(self, *args, **kwargs):
            yield ChatStreamEvent(type="token", content="started")
            await asyncio.Event().wait()

    terminal = None
    images = []
    with closing(dagent.Runner(provider=BlockingProvider(), workspace=tmp_path)) as runner:
        async with asyncio.timeout(5):
            async for event in runner.stream(agent(), input="look", persistence=dagent.StreamPersistenceOptions(),
                                            input_uploads=[dagent.ArtifactUpload("a", picture(), model_input="image")]):
                if event.type == "conversation.changed":
                    images.extend(resource for resource in event.data.resources if resource.reference.sha256)
                    if event.data.status is not None:
                        terminal = event.data
                    await runner.acknowledge_changes(event.data.run_id, event.data.batch_id)
                elif event.type == "response.content.delta":
                    await runner.cancel(event.run_id)
    assert terminal.status == "cancelled"
    assert terminal.conversation_schema_version == 5
    assert images
    assert Path(images[0].workspace_path, images[0].reference.path).exists()


def test_real_sandbox_image_transport(tmp_path):
    from tests.test_sandbox import _docker_live
    from dagent.capabilities.sandbox import SandboxSession

    if not _docker_live():
        pytest.skip("requires a running Docker daemon")
    source = tmp_path / "image.png"
    source.write_bytes(picture())
    session = SandboxSession(dagent.DockerSandboxConfig(), workspace_root=tmp_path)
    try:
        output = session.run_tool("read_image", {"path": source})
        assert base64.b64decode(output.images[0].data) == picture()
    finally:
        session.close()
