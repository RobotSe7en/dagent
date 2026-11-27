"""Opt-in visual inputs preserve released text and mutable-upload contracts."""

import base64
import hashlib
import json
from contextlib import closing
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

import dagent
from dagent.capabilities.images import ImageInputError
from dagent.harness_runtime.images import save_image, project_images
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.providers.model_io import (
    ModelRequest, ModelUserInput, ModelToolResultInput,
    model_request_to_chat, model_request_to_responses_input, chat_messages_to_model_request, image_content,
)
from tests.test_reasoning_mapping import _endpoint
from tests.test_cache_usage import _completion, _usage


def picture(color="red", *, format="PNG", size=(12, 8)):
    stream = BytesIO()
    Image.new("RGB", size, color).save(stream, format=format)
    return stream.getvalue()


def agent(**kwargs):
    return dagent.ToolAgent(profile="conversation", capabilities=kwargs.pop("capabilities", []), **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP", "GIF"])
async def test_visual_upload_is_explicit_and_portable(tmp_path, stream, format):
    data = picture(format=format)
    provider = MockProvider([ChatResponse(content="red")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        kwargs = dict(input="describe", input_uploads=[dagent.ArtifactUpload("image.bin", data, model_input="image")])
        if stream:
            events = [event async for event in runner.stream(agent(), **kwargs)]
            result = events[-1].data.result
        else:
            result = await runner.run(agent(), **kwargs)
    assert result.output_text == "red"
    assert result.conversation.schema_version == 5
    image = result.conversation.items[0].images[0]
    assert image.reference.sha256 == hashlib.sha256(data).hexdigest()
    assert (image.width, image.height) == (12, 8)
    assert (Path(result.state.workspace_path) / image.reference.path).read_bytes() == data
    parts = provider.requests[0]["messages"][-1]["content"]
    assert parts[0]["type"] == "text" and parts[-1]["type"] == "image_url"
    assert base64.b64decode(parts[-1]["image_url"]["url"].split(",")[1]) == data
    assert result.context_usage[-1].image_count == 1
    assert result.context_usage[-1].estimated_image_tokens == 8192
    serialized = result.model_dump(mode="json")
    assert "base64," not in json.dumps(serialized)
    assert dagent.RunResult.model_validate(serialized).conversation == result.conversation
    assert dagent.RunCheckpoint.model_validate_json(result.checkpoint.model_dump_json()) == result.checkpoint


@pytest.mark.asyncio
async def test_ordinary_uploads_keep_metadata_even_for_images(tmp_path):
    provider = MockProvider([ChatResponse(content="done")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        result = await runner.run(agent(), input="store", input_uploads=[dagent.ArtifactUpload("a.png", picture())])
    assert result.conversation.schema_version == 4
    assert not result.conversation.items[0].images
    assert "images" not in result.conversation.items[0].model_dump(mode="json")
    assert "image_count" not in result.context_usage[-1].model_dump(mode="json")
    assert isinstance(provider.requests[0]["messages"][-1]["content"], str)


@pytest.mark.asyncio
async def test_same_name_uploads_and_later_edits_do_not_change_seen_images(tmp_path):
    first, second = picture("red"), picture("blue")
    provider = MockProvider([ChatResponse(content="first"), ChatResponse(content="again")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        result = await runner.run(agent(), input="compare", input_uploads=[
            dagent.ArtifactUpload("a.png", first, model_input="image"),
            dagent.ArtifactUpload("a.png", second, model_input="image"),
            dagent.ArtifactUpload("note.txt", b"note"),
        ])
        workspace = Path(result.state.workspace_path)
        assert (workspace / "uploads/a.png").read_bytes() == second
        (workspace / "uploads/a.png").write_bytes(b"changed")
        await runner.run(agent(), input="again", conversation=result.conversation, workspace_path=workspace)
    image_parts = [part for part in provider.requests[-1]["messages"][1]["content"] if part["type"] == "image_url"]
    assert [base64.b64decode(part["image_url"]["url"].split(",")[1]) for part in image_parts] == [first, second]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,value", [("max_image_bytes", 10), ("max_image_pixels", 4),
                                         ("max_images_per_request", 1), ("max_total_image_bytes", 10)])
async def test_image_policy_limits_are_enforced(tmp_path, limit, value):
    provider = MockProvider()
    with closing(dagent.Runner(provider=provider, workspace=tmp_path,
                              image_input_policy=dagent.ImageInputPolicy(**{limit: value}))) as runner:
        with pytest.raises(ImageInputError):
            await runner.run(agent(), input="view", input_uploads=[
                dagent.ArtifactUpload("a", picture(), model_input="image"),
                dagent.ArtifactUpload("b", picture(), model_input="image"),
            ])
    assert not provider.requests


@pytest.mark.parametrize("payload,media_type", [(b"broken", None), (picture(), "image/jpeg")])
def test_bad_images_fail_explicitly(tmp_path, payload, media_type):
    with pytest.raises(ImageInputError):
        save_image(dagent.ImageData.from_bytes(payload, media_type=media_type), tmp_path)


def test_animation_and_snapshot_corruption_are_rejected(tmp_path):
    output = BytesIO()
    Image.new("RGB", (4, 4), "red").save(output, format="GIF", save_all=True,
                                       append_images=[Image.new("RGB", (4, 4), "blue")], duration=20)
    with pytest.raises(ImageInputError, match="Animated"):
        save_image(dagent.ImageData.from_bytes(output.getvalue()), tmp_path)
    image = save_image(dagent.ImageData.from_bytes(picture()), tmp_path)
    path = tmp_path / image.reference.path
    path.write_bytes(picture("blue"))
    with pytest.raises(ImageInputError, match="identity"):
        project_images((image,), tmp_path)
    path.unlink()
    with pytest.raises(ImageInputError, match="Cannot read"):
        project_images((image,), tmp_path)


@pytest.mark.asyncio
async def test_read_image_registration_and_parallel_order(tmp_path):
    (tmp_path / "a.png").write_bytes(picture())
    (tmp_path / "b.png").write_bytes(picture("blue"))
    provider = MockProvider([ChatResponse(tool_calls=[
        ToolCall(id="a", name="tool_read_image", arguments={"path": "a.png"}),
        ToolCall(id="b", name="tool_read_image", arguments={"path": "b.png"}),
    ]), ChatResponse(content="compared")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        assert runner.runtime.capability_catalog.get("tool.read_image") is None
        assert runner.add_image_tools()[0].id == "tool.read_image"
        assert len(runner.add_image_tools()) == 1
        result = await runner.run(agent(capabilities=["tool.read_image"], max_tool_concurrency=2),
                                  input="compare", workspace_path=tmp_path)
    assert result.conversation.schema_version == 5
    assert [len(item.images) for item in result.conversation.items if isinstance(item, dagent.ToolResultMessage)] == [1, 1]
    messages = provider.requests[-1]["messages"]
    assert [message["role"] for message in messages[-3:]] == ["tool", "tool", "user"]
    assert "call_id=a" in messages[-1]["content"][0]["text"]
    assert "call_id=b" in messages[-1]["content"][2]["text"]
    dagent.RunResult.model_validate(result.model_dump(mode="json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_native_provider_serializes_images_for_both_protocols(tmp_path, protocol, stream):
    async with _endpoint(protocol=protocol, responder=lambda request:
                         _completion(protocol, stream, usage=_usage(protocol), content="seen")) as (provider, posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
            kwargs = dict(input="see", input_uploads=[dagent.ArtifactUpload("a.png", picture(), model_input="image")])
            if stream:
                events = [event async for event in runner.stream(agent(), **kwargs)]
                assert events[-1].data.result.output_text == "seen"
            else:
                assert (await runner.run(agent(), **kwargs)).output_text == "seen"
    body = json.loads(posts[-1].content)
    content = body["messages" if protocol == "chat_completions" else "input"][-1]["content"]
    assert content[-1]["type"] == ("image_url" if protocol == "chat_completions" else "input_image")
    assert "detail" not in content[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
async def test_known_unsupported_vision_is_rejected_before_generation(tmp_path, protocol):
    from dagent.providers import ProviderCapabilityError

    async with _endpoint(protocol=protocol, responder=lambda request:
                         _completion(protocol, False, usage=_usage(protocol), content="unexpected")) as (provider, posts):
        capabilities = await provider.inspect_capabilities()
        provider._capabilities = capabilities.model_copy(update={protocol: getattr(capabilities, protocol).model_copy(
            update={"image_input": "unsupported"})})
        with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
            with pytest.raises(ProviderCapabilityError, match="image_input"):
                await runner.run(agent(), input="see", input_uploads=[
                    dagent.ArtifactUpload("a", picture(), model_input="image")])
    assert not posts


def test_legacy_chat_and_tool_output_projection(tmp_path):
    image = project_images((save_image(dagent.ImageData.from_bytes(picture()), tmp_path),), tmp_path)[0]
    original = [{"role": "user", "content": [{"type": "text", "text": "view"},
                {"type": "image_url", "image_url": {"url": image.data_url}}]}]
    assert model_request_to_chat(chat_messages_to_model_request(original)) == original
    interleaved = [{"role": "user", "content": [original[0]["content"][1],
                   {"type": "text", "text": "This image comes before its caption."},
                   original[0]["content"][1]]}]
    assert model_request_to_chat(chat_messages_to_model_request(interleaved)) == interleaved
    request = ModelRequest(instructions="", items=(ModelToolResultInput(
        source_id="result", call_id="call", name="read", content=image_content("image", (image,))),))
    output = model_request_to_responses_input(request)[0]
    assert output["call_id"] == "call"
    assert output["output"][1] == {"type": "input_image", "image_url": image.data_url}


@pytest.mark.asyncio
async def test_python_image_result_is_typed_and_snapshotted(tmp_path):
    from dagent.schemas import CapabilityDefinition

    async def handler(invocation):
        return dagent.CapabilityResult.completed(invocation, "screenshot", images=[dagent.ImageData.from_bytes(picture())])

    binding = dagent.CapabilityBinding(CapabilityDefinition(id="tool.screenshot", kind="tool"), handler)
    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="shot", name="tool_screenshot", arguments={})]),
                             ChatResponse(content="seen")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        result = await runner.run(agent(capabilities=[binding]), input="take screenshot")
    assert isinstance(result.conversation.items[-2].images[0], dagent.ImageReference)
    assert "base64," not in json.dumps(result.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_decorated_python_tool_can_return_image_data(tmp_path):
    @dagent.tool
    def screenshot() -> dagent.ImageData:
        return dagent.ImageData.from_bytes(picture())

    provider = MockProvider([ChatResponse(tool_calls=[ToolCall(id="s", name="tool_screenshot", arguments={})]),
                             ChatResponse(content="seen")])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        result = await runner.run(agent(capabilities=[screenshot]), input="capture")
    assert result.conversation.items[-2].images


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["auto", "dag"])
async def test_router_and_dynamic_planner_receive_visual_inputs(tmp_path, kind):
    from tests.planner_helpers import final_answer_response

    provider = MockProvider([ChatResponse(content="tool"), ChatResponse(content="seen")] if kind == "auto" else
                            [ChatResponse(content=final_answer_response("seen"))])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        target = dagent.AutoAgent(capabilities=[]) if kind == "auto" else dagent.DagAgent(capabilities=[])
        result = await runner.run(target, input="describe", input_uploads=[
            dagent.ArtifactUpload("a.png", picture(), model_input="image")])
    assert result.output_text == "seen"
    for request in provider.requests:
        assert request["messages"][1]["content"][-1]["type"] == "image_url"
    dagent.RunResult.model_validate(result.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_dynamic_dag_picture_result_reaches_planner_observation(tmp_path):
    from tests.planner_helpers import capability_plan_response, final_answer_response

    (tmp_path / "a.png").write_bytes(picture())
    provider = MockProvider([
        ChatResponse(content=capability_plan_response("tool.read_image", {"path": "a.png"})),
        ChatResponse(content=final_answer_response("seen")),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
        runner.add_image_tools()
        result = await runner.run(dagent.DagAgent(capabilities=["tool.read_image"]),
                                  input="read a.png", workspace_path=tmp_path)
    assert result.output_text == "seen"
    assert any(isinstance(message["content"], list) and message["content"][-1]["type"] == "image_url"
               for message in provider.requests[-1]["messages"])
    dagent.RunResult.model_validate(result.model_dump(mode="json"))


def test_image_estimation_preserves_literal_text_payloads(tmp_path):
    from dagent.providers.model_io import without_image_data
    from dagent.harness_runtime.context import ContextAssembler

    literal = "data:image/png;base64," + "x" * 10000
    payload = {"messages": [{"role": "user", "content": literal}]}
    assert without_image_data(payload) == payload
    image = project_images((save_image(dagent.ImageData.from_bytes(picture()), tmp_path),), tmp_path)[0]
    from dataclasses import replace
    from dagent.providers.model_io import image_content

    assembler = ContextAssembler()
    async def estimate(image):
        return await assembler._safe_estimate(ModelRequest(instructions="", items=(ModelUserInput(
            source_id="u", content=image_content("view", (image,))),)), dagent.ContextPolicy(), stream=False)
    import asyncio
    first, _ = asyncio.run(estimate(image))
    longer, _ = asyncio.run(estimate(replace(image, data_url=literal)))
    assert first == longer


@pytest.mark.asyncio
@pytest.mark.parametrize("covers_images", [False, True])
async def test_ordinary_token_count_preserves_image_coverage(tmp_path, covers_images):
    async with _endpoint(protocol="chat_completions", responder=lambda request:
                         _completion("chat_completions", False, usage=_usage("chat_completions"))) as (provider, _):
        capabilities = await provider.inspect_capabilities()
        provider._capabilities = capabilities.model_copy(update={"tokenize": "supported"})

        async def tokenize(path, body):
            return {"count": 512, "max_model_len": 32768, "includes_image_tokens": covers_images}

        provider._post_root_json = tokenize
        image = project_images((save_image(dagent.ImageData.from_bytes(picture()), tmp_path),), tmp_path)[0]
        count = await provider.count_tokens(ModelRequest(instructions="", items=(ModelUserInput(
            source_id="u", content=image_content("view", (image,))),)))
    assert count.includes_image_tokens is covers_images


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("strategy", ["chunked", "cache_aware"])
@pytest.mark.parametrize("covers_images", [False, True])
async def test_strict_prepared_visual_request_uses_covered_frozen_count(tmp_path, protocol, strategy, covers_images):
    from dagent.providers.model_io import ModelTokenCount, request_image_count

    async with _endpoint(protocol=protocol, responder=lambda request:
                         _completion(protocol, False, usage=_usage(protocol), content="seen")) as (native, posts):
        class CountedProvider:
            def __getattr__(self, name):
                return getattr(native, name)

            async def count_prepared(self, call):
                assert request_image_count(call.request) == 1
                return ModelTokenCount(count=512, exact=True, includes_image_tokens=covers_images)

        with closing(dagent.Runner(provider=CountedProvider(), workspace=tmp_path)) as runner:
            kwargs = dict(input="see", input_uploads=[dagent.ArtifactUpload("a", picture(), model_input="image")])
            configured = agent(context=dagent.ContextPolicy(compaction_strategy=strategy))
            if covers_images:
                result = await runner.run(configured, **kwargs)
                assert result.output_text == "seen"
                assert len(posts) == 1
            else:
                with pytest.raises(ImageInputError, match="counter") as failure:
                    await runner.run(configured, **kwargs)
                assert failure.value.result.conversation.items[0].images
                assert not posts


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("overlay", ["extra_body", "extra_request_args"])
async def test_overrides_cannot_silently_drop_selected_visual_input(tmp_path, protocol, overlay):
    from dagent.providers import ProviderCapabilityError

    key = "messages" if protocol == "chat_completions" else "input"
    async with _endpoint(protocol=protocol, **{overlay: {key: [{"role": "user", "content": "override"}]}},
                         responder=lambda request: _completion(protocol, False, usage=_usage(protocol))) as (provider, posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path)) as runner:
            with pytest.raises(ProviderCapabilityError, match="selected image"):
                await runner.run(agent(), input="view", input_uploads=[
                    dagent.ArtifactUpload("a", picture(), model_input="image")])
    # Budget discovery may tokenize; no generation may use the conflicting override.
    assert all(post.url.path.endswith("/tokenize") for post in posts)
