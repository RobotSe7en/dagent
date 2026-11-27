"""Visual validation and compaction retain evidence within a bounded context."""

from contextlib import closing
from pathlib import Path

import pytest

import dagent
from dagent.capabilities.images import ImageInputError, image_policy_context
from dagent.harness_runtime.compaction import HistoryCompactor
from dagent.harness_runtime.context import ContextAssembler
from dagent.harness_runtime.images import save_image
from dagent.harness_runtime.result_storage import ResultStore
from dagent.harness_runtime.validator_agent import ValidatorAgent
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider
from dagent.providers.model_io import ModelResponse, ModelTokenCount, text_only_request
from dagent.schemas.conversation import InlineContent, ResultObservation
from tests.planner_helpers import capability_plan_response, final_answer_response
from tests.test_compaction_strategy import summary_response
from tests.test_image_inputs import picture


READ_FILE = {"type": "function", "function": {"name": "tool_read_file"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [False, True])
async def test_validator_propagates_unavailable_image_evidence(tmp_path, corrupt):
    image = save_image(dagent.ImageData.from_bytes(picture()), tmp_path)
    snapshot = tmp_path / image.reference.path
    if corrupt:
        snapshot.write_bytes(b"corrupt")
    else:
        snapshot.unlink()
    provider = MockProvider()
    validator = ValidatorAgent(provider=provider, profile=AgentProfile(
        name="validator_agent", content="Check the answer against the evidence.",
    ))
    observation = ResultObservation(
        id="screenshot", name="screenshot", status="completed",
        content=InlineContent(text="Screenshot captured."), images=(image,),
    )

    with pytest.raises(ImageInputError):
        await validator.validate_with_audit(
            user_request="Describe the screenshot.", final_answer="It is red.",
            workspace_path=tmp_path, result_observations=(observation,),
            result_store=ResultStore(tmp_path, ".runtime"),
        )
    assert not provider.requests


@pytest.mark.asyncio
async def test_dynamic_dag_validator_sees_images_and_requests_correction(tmp_path):
    @dagent.tool
    def screenshot() -> dagent.ImageData:
        return dagent.ImageData.from_bytes(picture())

    provider = MockProvider([
        ChatResponse(content=capability_plan_response("tool.screenshot", {})),
        ChatResponse(content=final_answer_response("The square is blue.")),
        ChatResponse(content=final_answer_response("The square is red.")),
    ])
    validator_provider = MockProvider([
        ChatResponse(content='{"passed": false, "issues": [{"message": "The image is red."}], "summary": "Wrong color."}'),
    ])
    validator = ValidatorAgent(provider=validator_provider, profile=AgentProfile(
        name="validator_agent", content="Check the answer against the evidence.",
    ))
    with closing(dagent.Runner(provider=provider, validator=validator, workspace=tmp_path)) as runner:
        result = await runner.run(dagent.DagAgent(capabilities=[screenshot]), input="Describe the screenshot.")

    assert result.output_text == "The square is red."
    assert len(validator_provider.requests) == 1
    for request in validator_provider.requests:
        assert request["messages"][-1]["content"][-1]["type"] == "image_url"
    assert "The image is red." in str(provider.requests[-1]["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["chunked", "cache_aware"])
@pytest.mark.parametrize("retry_failed_source", [False, True])
async def test_visual_reference_fallback_fails_and_preserves_sources(tmp_path, strategy, retry_failed_source):
    image = save_image(dagent.ImageData.from_bytes(picture()), tmp_path)
    original = dagent.UserMessage(id="visual", content="Describe this image.", images=(image,))
    conversation = dagent.ConversationState(schema_version=5, items=(
        original, dagent.AssistantMessage(content="Earlier answer."),
        dagent.UserMessage(run_id="current", content="Continue."),
    ))

    async def count(request, _field):
        visual = any(getattr(item, "images", ()) for item in request.items)
        return ModelTokenCount(count=5000 if visual and request.purpose != "compaction" else 512,
                               exact=True, includes_image_tokens=True)

    async def fail(request):
        return ModelResponse(content="")

    policy = dagent.ContextPolicy(compaction_strategy=strategy, allow_reference_fallback=True)
    assembler = ContextAssembler(context_window_tokens=4096, max_output_tokens=512,
                                 request_token_counter=count)
    store = ResultStore(tmp_path, ".runtime")
    compactor = HistoryCompactor(policy=policy, assembler=assembler, result_store=store,
        system_message={"content": "Help."}, tools=(READ_FILE,), read_available=True, chat=fail)

    with image_policy_context(dagent.ImageInputPolicy(estimated_tokens_per_image=64)):
        with pytest.raises(ImageInputError, match="unsummarized images") as failure:
            if retry_failed_source:
                await compactor.reference_fallback(None, conversation.items[:2], 256, reason="Earlier failure.")
            else:
                await assembler.prepare(system_message={"content": "Help."}, conversation=conversation,
                    tools=(READ_FILE,), policy=policy, result_store=store,
                    history_compactor=compactor, enable_new_strategy=True)
    assert conversation.items[0] == original and conversation.summary is None
    if not retry_failed_source:
        assert failure.value._conversation.items == conversation.items
        assert failure.value._conversation.summary is None


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["legacy", "chunked", "cache_aware"])
async def test_many_archived_images_use_one_bounded_recovery_notice(tmp_path, strategy):
    images = tuple(save_image(dagent.ImageData.from_bytes(picture((index, 0, 0))), tmp_path)
                   for index in range(150))
    # Also covers restoring an older visual summary without an image index.
    conversation = dagent.ConversationState(schema_version=5,
        summary=dagent.ContextSummary(content="Earlier images were analyzed; continue the task.",
                                     source_item_count=300, images=images),
        items=(dagent.UserMessage(content="Continue."),))
    store = ResultStore(tmp_path, ".runtime")
    prepared = await ContextAssembler(context_window_tokens=4096, max_output_tokens=512).prepare(
        system_message={"content": "Help."}, conversation=conversation,
        policy=dagent.ContextPolicy(compaction_strategy=strategy, summary_max_tokens=128),
        result_store=store, enable_new_strategy=True,
    )
    summary = prepared.conversation.summary
    assert summary.images == images
    assert prepared.usage.estimated_input_tokens < 1024
    assert prepared.usage.summary_tokens < 256
    text = prepared.request.items[0].content
    assert summary.image_manifest.path in text
    assert not any(image.reference.path in text for image in images)
    archived = tuple(dagent.ImageReference.model_validate_json(line)
                     for line in store.read_reference(summary.image_manifest).splitlines())
    assert archived == images
    assert dagent.ConversationState.model_validate_json(prepared.conversation.model_dump_json()) == prepared.conversation


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["legacy", "chunked", "cache_aware"])
async def test_image_index_survives_repeated_compaction_and_text_fallback(tmp_path, strategy):
    store = ResultStore(tmp_path, ".runtime")
    assembler = ContextAssembler(context_window_tokens=8192)
    policy = dagent.ContextPolicy(compaction_strategy=strategy, allow_reference_fallback=True)
    summary = None
    images = []

    async def summarize(request):
        return summary_response(text_only_request(request))

    async def legacy_summarize(previous, items, limit):
        return dagent.ContextSummary(content="The images show different colors.",
            source_item_count=(previous.source_item_count if previous else 0) + len(items))

    async def count(request, field):
        return ModelTokenCount(count=512, exact=True, includes_image_tokens=True)

    assembler.request_token_counter = count
    for color in ("red", "blue"):
        image = save_image(dagent.ImageData.from_bytes(picture(color)), tmp_path)
        images.append(image)
        item = dagent.UserMessage(id=color, content="Describe this.", images=(image,))
        if strategy == "legacy":
            conversation = dagent.ConversationState(schema_version=5, summary=summary, items=(item,))
            updated, _, _ = await assembler._compact_slice(conversation, start=0, end=1,
                policy=policy, compact=legacy_summarize, result_store=store)
            summary = updated.summary
        else:
            compactor = HistoryCompactor(policy=policy, assembler=assembler, result_store=store,
                system_message={"content": "Help."}, read_available=True, chat=summarize)
            summary = await compactor.compact(summary, (item,), 256)

    text = dagent.UserMessage(id="text", content="Continue with the earlier images.")
    if strategy == "legacy":
        conversation = dagent.ConversationState(schema_version=5, summary=summary, items=(text,))
        updated, method, _ = await assembler._compact_slice(conversation, start=0, end=1,
            policy=policy, compact=None, result_store=store)
        summary = updated.summary
        assert method == "deterministic_fallback"
    else:
        summary = await compactor.reference_fallback(summary, (text,), 256, reason="offline")
        assert summary.method == "reference_fallback"
    assert summary.images == tuple(images)
    assert tuple(dagent.ImageReference.model_validate_json(line)
                 for line in store.read_reference(summary.image_manifest).splitlines()) == tuple(images)


@pytest.mark.asyncio
async def test_recovery_index_is_delivered_before_visual_continuation(tmp_path):
    image = save_image(dagent.ImageData.from_bytes(picture()), tmp_path)
    conversation = dagent.ConversationState(schema_version=5, summary=dagent.ContextSummary(
        content="Earlier image was red.", source_item_count=2, images=(image,),
    ))
    provider = MockProvider([ChatResponse(content="Continued.")])
    delivered = None
    replacement = None
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        async for event in runner.stream(dagent.ToolAgent(profile="conversation", capabilities=[]),
            input="Continue.", conversation=conversation, workspace_path=tmp_path,
            persistence=dagent.StreamPersistenceOptions()):
            if event.type != "conversation.changed":
                continue
            batch = event.data
            for resource in batch.resources:
                if resource.reference.path.startswith(".runtime/history/"):
                    assert not provider.requests
                    delivered = resource.reference
                    assert Path(resource.workspace_path, delivered.path).is_file()
            if batch.context and batch.context.summary_change:
                replacement = batch.context.summary_change.value
            await runner.acknowledge_changes(batch.run_id, batch.batch_id)
    assert delivered is not None
    assert replacement.image_manifest == delivered


def test_text_summary_serialization_keeps_released_shape():
    assert "image_manifest" not in dagent.ContextSummary(content="Earlier text.", source_item_count=2).model_dump()
