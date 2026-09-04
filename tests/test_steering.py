from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, AsyncIterator

import pytest

import dagent
from dagent.providers import ChatResponse, ChatStreamEvent, ToolCall
from dagent.providers.base import StructuredOutputFormat
from dagent.schemas import ToolResultMessage, UserMessage, ValidationResult
from tests.planner_helpers import final_answer_response


class GatedProvider:
    def __init__(
        self,
        responses: list[ChatResponse],
        *,
        blocked: set[int] | None = None,
    ) -> None:
        self.responses = responses
        self.blocked = blocked or set()
        self.started = [asyncio.Event() for _ in responses]
        self.release = [asyncio.Event() for _ in responses]
        self.requests: list[dict[str, Any]] = []
        self.transports: list[str] = []

    async def _respond(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        response_format: StructuredOutputFormat | None,
        transport: str,
    ) -> ChatResponse:
        index = len(self.requests)
        self.requests.append({
            "messages": list(messages),
            "tools": tools or [],
            "response_format": response_format,
        })
        self.transports.append(transport)
        self.started[index].set()
        if index in self.blocked:
            await self.release[index].wait()
        return self.responses[index]

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        response_format: StructuredOutputFormat | None = None,
    ) -> ChatResponse:
        return await self._respond(messages, tools, response_format, "chat")

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        response_format: StructuredOutputFormat | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        response = await self._respond(messages, tools, response_format, "stream")
        yield ChatStreamEvent(type="done", response=response)


class GatedCompactionProvider(GatedProvider):
    context_window_tokens = 2048
    max_output_tokens = 256

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        response_format: StructuredOutputFormat | None = None,
    ) -> ChatResponse:
        if str(messages[0].get("content", "")).startswith(
            "Summarize earlier conversation data"
        ):
            return ChatResponse(content="compacted conversation")
        return await super().chat(
            messages,
            tools,
            response_format=response_format,
        )


@pytest.mark.asyncio
async def test_runner_steer_applies_after_inflight_model_call(tmp_path) -> None:
    provider = GatedProvider(
        [ChatResponse(content="stale"), ChatResponse(content="steered")],
        blocked={0},
    )
    runner = dagent.Runner(
        runtime_directory=".runtime", workspace=tmp_path, provider=provider
    )
    events = []

    async def collect():
        async for event in runner.stream(
            dagent.ToolAgent(profile="conversation"),
            input="initial",
            run_id="steer_model_run",
        ):
            events.append(event)

    task = asyncio.create_task(collect())
    await provider.started[0].wait()
    receipt = await runner.steer("steer_model_run", "  use the new goal  ")
    provider.release[0].set()
    await task

    result = events[-1].data.result
    assert receipt.status == "queued"
    assert result.output_text == "steered"
    assert [event.type for event in events].count("steer.queued") == 1
    assert [event.type for event in events].count("steer.applied") == 1
    queued = next(event for event in events if event.type == "steer.queued")
    applied = next(event for event in events if event.type == "steer.applied")
    assert queued.data.steer_id == applied.data.steer_id == receipt.steer_id
    assert queued.data.content == "  use the new goal  "
    assert any(
        isinstance(item, UserMessage)
        and item.id == receipt.steer_id
        and item.content == "  use the new goal  "
        for item in result.state.model_thread.items
    )
    assert provider.requests[1]["messages"][-1]["content"] == "  use the new goal  "
    assert dagent.RunStreamEvent.model_validate(
        queued.model_dump(mode="json")
    ) == queued
    runner.close()


@pytest.mark.asyncio
async def test_runner_run_is_steerable_without_changing_chat_transport(tmp_path) -> None:
    provider = GatedProvider(
        [ChatResponse(content="stale"), ChatResponse(content="done")],
        blocked={0},
    )
    runner = dagent.Runner(
        runtime_directory=".runtime", workspace=tmp_path, provider=provider
    )
    task = asyncio.create_task(
        runner.run(
            dagent.ToolAgent(profile="conversation"),
            input="initial",
            run_id="direct_steer_run",
        )
    )
    await provider.started[0].wait()
    receipt = await runner.steer("direct_steer_run", "updated")
    provider.release[0].set()
    result = await task

    assert result.output_text == "done"
    assert provider.transports == ["chat", "chat"]
    assert provider.requests[1]["messages"][-1]["content"] == "updated"
    assert receipt.steer_id.startswith("steer_")
    with pytest.raises(dagent.RunNotActiveError):
        await runner.steer("direct_steer_run", "too late")
    runner.close()


@pytest.mark.asyncio
async def test_steer_waits_for_inflight_tool_and_skips_unstarted_calls(tmp_path) -> None:
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()
    calls: list[str] = []

    @dagent.tool(name="record")
    async def record(value: str) -> str:
        calls.append(value)
        if value == "first":
            tool_started.set()
            await release_tool.wait()
        return value

    provider = GatedProvider([
        ChatResponse(tool_calls=[
            ToolCall(id="call_1", name="record", arguments={"value": "first"}),
            ToolCall(id="call_2", name="record", arguments={"value": "second"}),
        ]),
        ChatResponse(content="redirected"),
    ])
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        capabilities=[record],
    )
    events = []

    async def collect():
        async for event in runner.stream(
            dagent.ToolAgent(profile="conversation", capabilities=[record]),
            input="record twice",
            run_id="steer_tool_run",
        ):
            events.append(event)

    task = asyncio.create_task(collect())
    await tool_started.wait()
    receipt = await runner.steer("steer_tool_run", "stop after the first")
    assert calls == ["first"]
    release_tool.set()
    await task

    result = events[-1].data.result
    assert calls == ["first"]
    assert result.output_text == "redirected"
    skipped = [
        item
        for item in result.state.model_thread.items
        if isinstance(item, ToolResultMessage) and item.call_id == "call_2"
    ]
    assert len(skipped) == 1
    assert "steer" in skipped[0].content.text.lower()
    assert any(
        event.type == "steer.applied" and event.data.steer_id == receipt.steer_id
        for event in events
    )
    runner.close()


@pytest.mark.asyncio
async def test_auto_agent_can_be_steered_after_tool_route_resolves(tmp_path) -> None:
    provider = GatedProvider(
        [
            ChatResponse(content="tool"),
            ChatResponse(content="stale"),
            ChatResponse(content="auto redirected"),
        ],
        blocked={1},
    )
    runner = dagent.Runner(
        runtime_directory=".runtime", workspace=tmp_path, provider=provider
    )
    events = []

    async def collect():
        async for event in runner.stream(
            dagent.AutoAgent(), input="help", run_id="auto_tool_steer"
        ):
            events.append(event)

    task = asyncio.create_task(collect())
    await provider.started[1].wait()
    await runner.steer("auto_tool_steer", "change direction")
    provider.release[1].set()
    await task

    assert events[0].type == "run.started"
    assert events[0].data.kind == "tool"
    assert events[-1].data.result.output_text == "auto redirected"
    assert provider.requests[2]["messages"][-1]["content"] == "change direction"
    runner.close()


@pytest.mark.asyncio
async def test_auto_agent_dag_route_rejects_steer(tmp_path) -> None:
    provider = GatedProvider(
        [
            ChatResponse(content="dag"),
            ChatResponse(content=final_answer_response("done")),
        ],
        blocked={1},
    )
    runner = dagent.Runner(
        runtime_directory=".runtime", workspace=tmp_path, provider=provider
    )
    events = []

    async def collect():
        async for event in runner.stream(
            dagent.AutoAgent(), input="plan it", run_id="auto_dag_no_steer"
        ):
            events.append(event)

    task = asyncio.create_task(collect())
    await provider.started[1].wait()
    with pytest.raises(dagent.RunNotSteerableError, match="not a tool-agent"):
        await runner.steer("auto_dag_no_steer", "change direction")
    provider.release[1].set()
    await task

    assert events[0].data.kind == "dynamic_dag"
    assert not any(event.type.startswith("steer.") for event in events)
    runner.close()


@pytest.mark.asyncio
async def test_root_steer_waits_for_nested_tool_agent_to_return(tmp_path) -> None:
    provider = GatedProvider(
        [
            ChatResponse(tool_calls=[
                ToolCall(
                    id="call_1",
                    name="agent_helper",
                    arguments={"prompt": "summarize"},
                )
            ]),
            ChatResponse(content="helper answer"),
            ChatResponse(content="root redirected"),
        ],
        blocked={1},
    )
    helper = dagent.ToolAgent(
        profile="conversation",
        name="helper",
        max_steps=1,
        capabilities=[],
        skills=[],
    )
    runner = dagent.Runner(
        runtime_directory=".runtime", workspace=tmp_path, provider=provider
    )
    events = []

    async def collect():
        async for event in runner.stream(
            dagent.ToolAgent(
                profile="conversation",
                capabilities=[],
                skills=[],
                agents=[helper],
            ),
            input="delegate",
            run_id="root_subagent_steer",
        ):
            events.append(event)

    task = asyncio.create_task(collect())
    await provider.started[1].wait()
    receipt = await runner.steer("root_subagent_steer", "change the root response")
    provider.release[1].set()
    await task

    assert events[-1].data.result.output_text == "root redirected"
    assert not any(
        message.get("content") == "change the root response"
        for message in provider.requests[1]["messages"]
    )
    assert any(
        message.get("content") == "change the root response"
        for message in provider.requests[2]["messages"]
    ), provider.requests[2]["messages"]
    assert any(
        event.type == "steer.applied" and event.data.steer_id == receipt.steer_id
        for event in events
    )
    runner.close()


@pytest.mark.asyncio
async def test_steer_queue_is_bounded_and_fifo(tmp_path) -> None:
    provider = GatedProvider(
        [ChatResponse(content="stale"), ChatResponse(content="done")],
        blocked={0},
    )
    runner = dagent.Runner(
        runtime_directory=".runtime", workspace=tmp_path, provider=provider
    )
    task = asyncio.create_task(
        runner.run(
            dagent.ToolAgent(profile="conversation"),
            input="initial",
            run_id="bounded_steer_queue",
        )
    )
    await provider.started[0].wait()
    receipts = [
        await runner.steer("bounded_steer_queue", f"update {index}")
        for index in range(32)
    ]
    with pytest.raises(dagent.SteerQueueFullError):
        await runner.steer("bounded_steer_queue", "overflow")
    provider.release[0].set()
    result = await task

    applied = [
        item
        for item in result.state.model_thread.items
        if isinstance(item, UserMessage) and item.id.startswith("steer_")
    ]
    assert [item.id for item in applied] == [receipt.steer_id for receipt in receipts]
    assert [item.content for item in applied] == [
        f"update {index}" for index in range(32)
    ]
    runner.close()


@pytest.mark.asyncio
async def test_steer_at_step_limit_is_discarded_and_run_fails(tmp_path) -> None:
    provider = GatedProvider([ChatResponse(content="stale")], blocked={0})
    runner = dagent.Runner(
        runtime_directory=".runtime", workspace=tmp_path, provider=provider
    )
    events = []

    async def collect():
        async for event in runner.stream(
            dagent.ToolAgent(profile="conversation", max_steps=1),
            input="initial",
            run_id="steer_step_limit",
        ):
            events.append(event)

    task = asyncio.create_task(collect())
    await provider.started[0].wait()
    receipt = await runner.steer("steer_step_limit", "must retry")
    provider.release[0].set()
    await task

    discarded = next(event for event in events if event.type == "steer.discarded")
    result = events[-1].data.result
    assert discarded.data.steer_id == receipt.steer_id
    assert discarded.data.reason == "step_limit_exhausted"
    assert result.status == "failed"
    assert result.output_text == (
        "The task could not apply a user steering update because the maximum model "
        "steps were exhausted. The preceding assistant response was discarded."
    )
    assert "stale" not in result.output_text
    assert not any(
        isinstance(item, UserMessage) and item.id == receipt.steer_id
        for item in result.state.model_thread.items
    )
    runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_resume_and_resume_stream_accept_steer_after_review_approval(
    tmp_path,
    streaming: bool,
) -> None:
    tool_started = asyncio.Event()
    release_tool = asyncio.Event()

    @dagent.tool(name="write", risk="medium")
    async def write(value: str) -> str:
        tool_started.set()
        await release_tool.wait()
        return value

    provider = GatedProvider([
        ChatResponse(tool_calls=[
            ToolCall(id="call_1", name="write", arguments={"value": "x"})
        ]),
        ChatResponse(content="resumed and redirected"),
    ])
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        capabilities=[write],
    )
    first = await runner.run(
        dagent.ToolAgent(
            profile="conversation", capabilities=[write], review="careful"
        ),
        input="write",
        run_id=f"resume_steer_{streaming}",
    )
    assert first.review is not None
    assert first.checkpoint is not None
    events = []

    if streaming:
        async def collect():
            async for event in runner.resume_stream(
                first.review.approve(), checkpoint=first.checkpoint
            ):
                events.append(event)

        resume_task = asyncio.create_task(collect())
    else:
        resume_task = asyncio.create_task(
            runner.resume(first.review.approve(), checkpoint=first.checkpoint)
        )

    await tool_started.wait()
    receipt = await runner.steer(first.run_id, "finish with the updated direction")
    release_tool.set()
    resumed = await resume_task

    result = events[-1].data.result if streaming else resumed
    assert result is not None
    assert result.output_text == "resumed and redirected"
    assert provider.requests[1]["messages"][-1]["content"] == (
        "finish with the updated direction"
    )
    if streaming:
        assert any(
            event.type == "steer.applied" and event.data.steer_id == receipt.steer_id
            for event in events
        )
    runner.close()


@pytest.mark.asyncio
async def test_validator_receives_initial_request_plus_applied_steers(tmp_path) -> None:
    provider = GatedProvider(
        [ChatResponse(content="stale"), ChatResponse(content="final")],
        blocked={0},
    )
    captured_request: str | None = None
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        validator="validator_agent",
    )

    async def capture_validation(*, user_request: str, **_kwargs):
        nonlocal captured_request
        captured_request = user_request
        return ValidationResult(passed=True, summary="ok"), None, None

    assert runner.runtime.validator is not None
    runner.runtime.validator.validate_with_audit = capture_validation  # type: ignore[method-assign]
    task = asyncio.create_task(
        runner.run(
            dagent.ToolAgent(profile="conversation"),
            input="original request",
            run_id="validator_effective_request",
        )
    )
    await provider.started[0].wait()
    await runner.steer("validator_effective_request", "first update")
    await runner.steer("validator_effective_request", "second update")
    provider.release[0].set()
    result = await task

    assert result.state.user_request == "original request"
    assert captured_request == (
        "original request\n\n"
        "User steering updates (chronological):\n"
        "1. first update\n"
        "2. second update"
    )
    runner.close()


@pytest.mark.asyncio
async def test_validator_preserves_applied_steers_across_compaction_and_retry(
    tmp_path,
) -> None:
    provider = GatedCompactionProvider(
        [
            ChatResponse(content="stale one"),
            ChatResponse(content="stale two"),
            ChatResponse(content="first final"),
            ChatResponse(content="corrected final"),
        ],
        blocked={0, 1},
    )
    captured_requests: list[str] = []
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        validator="validator_agent",
    )

    async def capture_validation(*, user_request: str, **_kwargs):
        captured_requests.append(user_request)
        return (
            ValidationResult(
                passed=len(captured_requests) > 1,
                summary="retry once",
            ),
            None,
            None,
        )

    assert runner.runtime.validator is not None
    runner.runtime.validator.validate_with_audit = capture_validation  # type: ignore[method-assign]
    task = asyncio.create_task(
        runner.run(
            dagent.ToolAgent(
                profile="conversation",
                context=dagent.ContextPolicy(
                    compaction_trigger_ratio=0.2,
                    summary_max_tokens=64,
                ),
            ),
            input="original request " * 40,
            run_id="validator_compacted_steers",
        )
    )
    await provider.started[0].wait()
    await runner.steer("validator_compacted_steers", "first update")
    provider.release[0].set()
    await provider.started[1].wait()
    await runner.steer("validator_compacted_steers", "second update")
    provider.release[1].set()
    result = await task

    effective_request = (
        f"{'original request ' * 40}\n\n"
        "User steering updates (chronological):\n"
        "1. first update\n"
        "2. second update"
    )
    assert captured_requests == [effective_request, effective_request]
    assert result.output_text == "corrected final"
    assert not any(
        isinstance(item, UserMessage) and item.content == "first update"
        for item in result.state.model_thread.items
    )
    runner.close()


@pytest.mark.asyncio
async def test_validation_phase_rejects_steer(tmp_path) -> None:
    validation_started = asyncio.Event()
    release_validation = asyncio.Event()
    provider = GatedProvider([ChatResponse(content="done")])
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        validator="validator_agent",
    )

    async def block_validation(**_kwargs):
        validation_started.set()
        await release_validation.wait()
        return ValidationResult(passed=True, summary="ok"), None, None

    assert runner.runtime.validator is not None
    runner.runtime.validator.validate_with_audit = block_validation  # type: ignore[method-assign]
    task = asyncio.create_task(
        runner.run(
            dagent.ToolAgent(profile="conversation"),
            input="initial",
            run_id="validation_no_steer",
        )
    )
    await validation_started.wait()
    with pytest.raises(dagent.RunNotSteerableError, match="validation"):
        await runner.steer("validation_no_steer", "too late")
    release_validation.set()
    await task
    runner.close()


@pytest.mark.asyncio
async def test_failed_run_discards_queued_steer(tmp_path) -> None:
    class FailingProvider:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def chat(self, messages, tools=None, *, response_format=None):
            self.started.set()
            await self.release.wait()
            raise RuntimeError("provider failed")

        async def stream_chat(
            self, messages, tools=None, *, response_format=None
        ) -> AsyncIterator[ChatStreamEvent]:
            await self.chat(messages, tools=tools, response_format=response_format)
            if False:  # pragma: no cover - keeps this an async generator
                yield ChatStreamEvent(type="done", response=ChatResponse())

    provider = FailingProvider()
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
    )
    events = []

    async def collect():
        async for event in runner.stream(
            dagent.ToolAgent(profile="conversation"),
            input="fail",
            run_id="failed_steer_run",
        ):
            events.append(event)

    task = asyncio.create_task(collect())
    await provider.started.wait()
    receipt = await runner.steer("failed_steer_run", "recover differently")
    provider.release.set()
    await task

    discarded = next(event for event in events if event.type == "steer.discarded")
    assert discarded.data.steer_id == receipt.steer_id
    assert discarded.data.reason == "run_failed"
    assert events[-1].type == "run.failed"
    runner.close()


@pytest.mark.asyncio
async def test_cancel_discards_queued_steer(tmp_path) -> None:
    provider = GatedProvider([ChatResponse(content="unused")], blocked={0})
    runner = dagent.Runner(
        runtime_directory=".runtime", workspace=tmp_path, provider=provider
    )
    events = []

    async def collect():
        with suppress(asyncio.CancelledError):
            async for event in runner.stream(
                dagent.ToolAgent(profile="conversation"),
                input="wait",
                run_id="cancel_steer_run",
            ):
                events.append(event)

    task = asyncio.create_task(collect())
    await provider.started[0].wait()
    receipt = await runner.steer("cancel_steer_run", "queued before cancel")
    assert await runner.cancel("cancel_steer_run") is True
    await task

    discarded = next(event for event in events if event.type == "steer.discarded")
    assert discarded.data.steer_id == receipt.steer_id
    assert discarded.data.reason == "run_cancelled"
    runner.close()


@pytest.mark.asyncio
async def test_runner_close_discards_queued_steer(tmp_path) -> None:
    provider = GatedProvider([ChatResponse(content="unused")], blocked={0})
    runner = dagent.Runner(
        runtime_directory=".runtime", workspace=tmp_path, provider=provider
    )
    events = []

    async def collect():
        with suppress(asyncio.CancelledError):
            async for event in runner.stream(
                dagent.ToolAgent(profile="conversation"),
                input="wait",
                run_id="close_steer_run",
            ):
                events.append(event)

    task = asyncio.create_task(collect())
    await provider.started[0].wait()
    receipt = await runner.steer("close_steer_run", "queued before close")
    runner.close()
    await task

    discarded = next(event for event in events if event.type == "steer.discarded")
    assert discarded.data.steer_id == receipt.steer_id
    assert discarded.data.reason == "runner_closed"


@pytest.mark.asyncio
async def test_awaiting_review_rejects_steer_in_favor_of_review_feedback(tmp_path) -> None:
    @dagent.tool(name="write", risk="medium")
    def write(value: str) -> str:
        return value

    provider = GatedProvider([
        ChatResponse(tool_calls=[
            ToolCall(id="call_1", name="write", arguments={"value": "x"})
        ])
    ])
    runner = dagent.Runner(
        runtime_directory=".runtime",
        workspace=tmp_path,
        provider=provider,
        capabilities=[write],
    )
    result = await runner.run(
        dagent.ToolAgent(
            profile="conversation", capabilities=[write], review="careful"
        ),
        input="write",
        run_id="review_no_steer",
    )

    assert result.status == "awaiting_review"
    with pytest.raises(dagent.RunNotSteerableError, match="review feedback"):
        await runner.steer("review_no_steer", "approve this")
    runner.close()


@pytest.mark.asyncio
async def test_runner_steer_validates_active_run_and_text(tmp_path) -> None:
    runner = dagent.Runner(
        runtime_directory=".runtime", workspace=tmp_path, provider=GatedProvider([])
    )
    with pytest.raises(dagent.RunNotActiveError):
        await runner.steer("missing_run", "update")
    with pytest.raises(TypeError, match="string"):
        await runner.steer("missing_run", 123)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="empty"):
        await runner.steer("missing_run", " \n ")
    runner.close()
