"""Visual history is summarized before transport or context limits reject it."""

import json
from contextlib import closing

import pytest

import dagent
from dagent.capabilities.images import ImageInputError, image_policy_context
from dagent.harness_runtime.compaction import HistoryCompactor
from dagent.harness_runtime.context import ContextAssembler
from dagent.harness_runtime.images import save_image, source_images
from dagent.harness_runtime.result_storage import ResultStore
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.providers.model_io import (
    ModelRequest, ModelTokenCount, ModelUserInput,
    request_image_count, text_only_request, validate_request_images,
)
from dagent.schemas.conversation import InlineContent
from tests.test_cache_usage import _completion, _usage
from tests.test_compaction_strategy import summary_response
from tests.test_context_prefix import _assert_prefix
from tests.test_image_inputs import picture
from tests.test_reasoning_mapping import _endpoint


@pytest.mark.asyncio
async def test_uncovered_image_count_still_enforces_discovered_server_window(tmp_path):
    async with _endpoint(protocol="chat_completions", token_counting="vllm") as (provider, posts):
        async def tokenize(path, body):
            assert path == "/tokenize"
            return {"count": 5000, "max_model_len": 4096}

        provider._post_root_json = tokenize
        with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
            with pytest.raises(dagent.ContextWindowExceeded) as failure:
                await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[]),
                    input="long " * 4000, input_uploads=[
                        dagent.ArtifactUpload("a.png", picture(), model_input="image")])
    assert failure.value.usage.context_window_tokens == 4096
    assert failure.value.usage.server_max_model_len == 4096
    assert not posts


@pytest.mark.asyncio
async def test_default_legacy_image_conversation_continues_past_fourteen_turns(tmp_path):
    provider = MockProvider([ChatResponse(content="The image was analyzed.")] * 50)
    conversation = None
    compacted = 0
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        for index in range(20):
            result = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[]),
                input="Describe this image.", conversation=conversation, workspace_path=tmp_path,
                input_uploads=[dagent.ArtifactUpload("image.png", picture((index, 0, 0)), model_input="image")])
            assert result.status == "completed"
            compacted += result.context_usage[-1].compacted_items
            conversation = result.conversation
    assert compacted > 0
    assert conversation.summary.images
    assert len({image.reference.sha256 for image in
                (*conversation.summary.images, *source_images(conversation.items))}) == 20
    # Archived images stay in the index, rather than being reattached to the request.
    summary_message = provider.requests[-1]["messages"][1]["content"]
    assert isinstance(summary_message, str)
    assert conversation.summary.image_manifest.path in summary_message


def _limited_policy(limit, data):
    return dagent.ImageInputPolicy(
        max_images_per_request=2 if limit == "count" else 16,
        max_total_image_bytes=2 * len(data) if limit == "bytes" else 100 * 1024 * 1024,
        estimated_tokens_per_image=64,
    )


def _visual_history(image):
    return dagent.ConversationState(schema_version=5, items=(
        *(item for index in range(4) for item in (
            dagent.UserMessage(run_id=f"old_{index}", content="Look.", images=(image,)),
            dagent.AssistantMessage(run_id=f"old_{index}", content="Analyzed."),
        )),
        dagent.UserMessage(run_id="current", content="Continue.", images=(image,)),
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["count", "bytes"])
async def test_legacy_batches_visual_history_before_transport_limits(tmp_path, limit):
    data = picture()
    image = save_image(dagent.ImageData.from_bytes(data), tmp_path)
    history = _visual_history(image)
    # Each visual summary may contain at most two of the four older images.
    provider = MockProvider([ChatResponse(content="Earlier images were red.")] * 10)
    provider.configured_context_window_tokens = 262144
    with closing(dagent.Runner(provider=provider, workspace=tmp_path,
                              image_input_policy=_limited_policy(limit, data))) as runner:
        result = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[]),
            input="One more turn.", conversation=history.model_copy(update={"items": history.items[:-1]}),
            workspace_path=tmp_path, input_uploads=[dagent.ArtifactUpload("a.png", data, model_input="image")])
    assert result.status == "completed"
    assert result.context_usage[-1].compacted_items > 0
    counts = [sum(part.get("type") == "image_url" for message in request["messages"]
                  if isinstance(message.get("content"), list) for part in message["content"])
              for request in provider.requests]
    assert counts == [2, 2, 1]
    assert result.conversation.summary.images == (image,)


@pytest.mark.asyncio
async def test_legacy_visual_batches_reserve_previous_summary_and_safety_margin(tmp_path):
    image = save_image(dagent.ImageData.from_bytes(picture()), tmp_path)
    history = dagent.ConversationState(schema_version=5,
        summary=dagent.ContextSummary(content="Earlier decisions. " * 1500, source_item_count=2),
        items=tuple(item for index in range(17) for item in (
            dagent.UserMessage(run_id=f"old_{index}", content="Look.", images=(image,)),
            dagent.AssistantMessage(run_id=f"old_{index}", content="Analyzed."),
        )))
    provider = MockProvider([ChatResponse(content="The earlier images were red.")] * 10)
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        result = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[],
            context=dagent.ContextPolicy(token_safety_margin=1)), input="Continue.",
            conversation=history, workspace_path=tmp_path,
            input_uploads=[dagent.ArtifactUpload("a.png", picture(), model_input="image")])
    assert result.status == "completed"
    assert result.context_usage[-1].image_count == 1
    assert result.conversation.summary.source_item_count == 36
    assert len(provider.requests) > 2


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["chunked", "cache_aware"])
@pytest.mark.parametrize("limit", ["count", "bytes"])
async def test_strict_strategies_compact_aggregate_image_overflow_before_counting(tmp_path, strategy, limit):
    data = picture()
    image = save_image(dagent.ImageData.from_bytes(data), tmp_path)
    conversation = _visual_history(image)
    requests = []

    async def count(request, field):
        # Over-limit planning views must never be sent to a provider counter.
        validate_request_images(request)
        return ModelTokenCount(count=512, exact=True, includes_image_tokens=True)

    async def summarize(request):
        validate_request_images(request)
        requests.append(request)
        return summary_response(text_only_request(request))

    policy = dagent.ContextPolicy(compaction_strategy=strategy)
    assembler = ContextAssembler(context_window_tokens=262144, max_output_tokens=512,
                                 request_token_counter=count)
    store = ResultStore(tmp_path, ".runtime")
    compactor = HistoryCompactor(policy=policy, assembler=assembler, result_store=store,
        system_message={"content": "Help."}, chat=summarize)
    with image_policy_context(_limited_policy(limit, data)):
        prepared = await assembler.prepare(system_message={"content": "Help."}, conversation=conversation,
            policy=policy, active_run_id="current", result_store=store,
            history_compactor=compactor, enable_new_strategy=True)
        validate_request_images(prepared.request)
    assert prepared.usage.compacted_items > 0
    assert prepared.usage.image_count == 1
    assert prepared.conversation.summary.images == (image,)
    assert sum(request_image_count(request) for request in requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["chunked", "cache_aware"])
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("limit", ["count", "bytes"])
async def test_runner_strict_visual_overflow_uses_bounded_summary_requests(tmp_path, strategy, protocol, limit):
    data = picture()
    image = save_image(dagent.ImageData.from_bytes(data), tmp_path)
    history = _visual_history(image)
    history = history.model_copy(update={"items": history.items[:-1]})
    counted = []

    def responder(request):
        body = json.loads(request.content)
        last = body["messages" if protocol == "chat_completions" else "input"][-1]
        content = last["content"]
        text = content if isinstance(content, str) else content[0]["text"]
        if "[Source S" in text or "Sources to summarize:" in text:
            answer = summary_response(ModelRequest(instructions="", items=(
                ModelUserInput(source_id="sources", content=text),))).content
        else:
            answer = "Seen."
        return _completion(protocol, body.get("stream", False), usage=_usage(protocol), content=answer)

    async with _endpoint(protocol=protocol, context_window_tokens=262144, max_output_tokens=512,
                         token_counting="heuristic", responder=responder) as (native, _):
        class CountedProvider:
            def __getattr__(self, name):
                return getattr(native, name)

            async def count_prepared(self, call):
                validate_request_images(call.request)
                counted.append(call)
                return ModelTokenCount(count=512, exact=True, includes_image_tokens=True)

        with closing(dagent.Runner(provider=CountedProvider(), workspace=tmp_path,
                                  image_input_policy=_limited_policy(limit, data))) as runner:
            result = await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[],
                context=dagent.ContextPolicy(compaction_strategy=strategy)), input="Continue.",
                conversation=history, workspace_path=tmp_path,
                input_uploads=[dagent.ArtifactUpload("a.png", data, model_input="image")])
    assert result.status == "completed"
    assert result.context_usage[-1].image_count == 1
    assert result.conversation.summary.images == (image,)
    assert any(call.request.purpose == "compaction" and request_image_count(call.request) for call in counted)


@pytest.mark.asyncio
async def test_legacy_visual_summary_batches_keep_tool_calls_with_their_results(tmp_path):
    image = save_image(dagent.ImageData.from_bytes(picture()), tmp_path)
    items = tuple(item for name in ("first", "second") for item in (
        dagent.AssistantMessage(id=f"call_{name}", tool_calls=(
            dagent.ToolCallItem(id=name, name="screenshot"),)),
        dagent.ToolResultMessage(id=f"result_{name}", call_id=name, name="screenshot",
            status="completed", content=InlineContent(text="Captured."), images=(image,)),
    ))
    batches = []

    async def compact(previous, batch, limit):
        batches.append(tuple(item.id for item in batch))
        return dagent.ContextSummary(content="The screenshots were red.",
            source_item_count=(previous.source_item_count if previous else 0) + len(batch))

    with image_policy_context(dagent.ImageInputPolicy(max_images_per_request=1)):
        updated, _, _ = await ContextAssembler(context_window_tokens=262144)._compact_slice(
            dagent.ConversationState(schema_version=5, items=items), start=0, end=len(items),
            policy=dagent.ContextPolicy(), compact=compact, result_store=ResultStore(tmp_path, ".runtime"))
    assert batches == [("call_first", "result_first"), ("call_second", "result_second")]
    assert updated.summary.source_item_count == 4
    assert updated.summary.images == (image,)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["legacy", "chunked", "cache_aware"])
@pytest.mark.parametrize("limit", ["count", "bytes"])
async def test_protected_image_overflow_fails_without_counting_or_losing_sources(tmp_path, strategy, limit):
    data = picture()
    image = save_image(dagent.ImageData.from_bytes(data), tmp_path)
    conversation = dagent.ConversationState(schema_version=5, items=(
        dagent.UserMessage(run_id="current", content="Look.", images=(image,) * 3),
    ))

    async def count(*args):
        pytest.fail("An over-limit current input must not reach the provider counter")

    assembler = ContextAssembler(context_window_tokens=262144, max_output_tokens=512,
                                 request_token_counter=count)
    with image_policy_context(_limited_policy(limit, data)):
        with pytest.raises(ImageInputError, match="max_images_per_request|max_total_image_bytes"):
            await assembler.prepare(system_message={"content": "Help."}, conversation=conversation,
                policy=dagent.ContextPolicy(compaction_strategy=strategy),
                result_store=ResultStore(tmp_path, ".runtime"), enable_new_strategy=True)
    assert conversation.summary is None and conversation.items[0].images == (image,) * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["chunked", "cache_aware"])
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_strict_small_window_uses_exact_image_count_despite_large_estimate(tmp_path, strategy, protocol, stream):
    counted = []
    async with _endpoint(protocol=protocol, context_window_tokens=4096, max_output_tokens=512,
        token_counting="heuristic", responder=lambda request:
            _completion(protocol, stream, usage=_usage(protocol), content="Seen.")) as (native, posts):
        class CountedProvider:
            def __getattr__(self, name):
                return getattr(native, name)

            async def count_prepared(self, call):
                counted.append(call)
                return ModelTokenCount(count=300, exact=True, includes_image_tokens=True)

        with closing(dagent.Runner(provider=CountedProvider(), workspace=tmp_path)) as runner:
            target = dagent.ToolAgent(profile="conversation", capabilities=[],
                context=dagent.ContextPolicy(compaction_strategy=strategy))
            arguments = dict(input="Look.", input_uploads=[
                dagent.ArtifactUpload("a.png", picture(), model_input="image")])
            if stream:
                events = [event async for event in runner.stream(target, **arguments)]
                result = events[-1].data.result
            else:
                result = await runner.run(target, **arguments)
    assert result.status == "completed" and counted
    assert result.context_usage[-1].estimated_input_tokens == 300
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_visual_summary_uses_exact_count_despite_large_estimate(tmp_path):
    image = save_image(dagent.ImageData.from_bytes(picture()), tmp_path)
    counted = []
    async with _endpoint(protocol="chat_completions", context_window_tokens=4096,
                         max_output_tokens=512, token_counting="heuristic") as (provider, _):
        async def count(call):
            counted.append(call)
            return ModelTokenCount(count=300, exact=True, includes_image_tokens=True)

        async def summarize(request):
            return summary_response(text_only_request(request))

        assembler = ContextAssembler(context_window_tokens=4096, max_output_tokens=512,
            request_preparer=provider.prepare_call, prepared_token_counter=count)
        compactor = HistoryCompactor(policy=dagent.ContextPolicy(compaction_strategy="chunked"),
            assembler=assembler, result_store=ResultStore(tmp_path, ".runtime"),
            system_message={"content": "Help."}, chat=summarize)
        summary = await compactor.compact(None, (
            dagent.UserMessage(content="Describe this image.", images=(image,)),), 256)
    assert counted and summary.images == (image,)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_visual_upload_tool_result_and_continuation_keep_wire_prefix(tmp_path, protocol):
    calls = 0

    @dagent.tool
    def screenshot() -> dagent.ImageData:
        return dagent.ImageData.from_bytes(picture("blue"))

    def responder(request):
        nonlocal calls
        calls += 1
        return _completion(protocol, False, usage=_usage(protocol),
            content="" if calls == 1 else "Seen.", calls=(
                ToolCall(id="shot", name="tool_screenshot", arguments={}),) if calls == 1 else ())

    async with _endpoint(protocol=protocol, token_counting="heuristic", responder=responder) as (provider, posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
            target = dagent.ToolAgent(profile="conversation", capabilities=[screenshot],
                context=dagent.ContextPolicy(reasoning_replay="all_runs"))
            first = await runner.run(target, input="Look and capture.", workspace_path=tmp_path,
                input_uploads=[dagent.ArtifactUpload("a.png", picture(), model_input="image")])
            await runner.run(target, input="Continue.", conversation=first.conversation, workspace_path=tmp_path)
    bodies = [json.loads(post.content) for post in posts]
    assert len(bodies) == 3
    _assert_prefix(bodies[0], bodies[1], protocol)
    _assert_prefix(bodies[1], bodies[2], protocol)
