"""Unit tests for the chat display gate used by the SSE message endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from typing import Any

from api.stream_gate import gate_chat_display
from dagent.result import (
    CapabilityCallCompletedData,
    CapabilityCallStartedData,
    ResponseFinishedData,
    ResponseStartedData,
    RunFinishedData,
    RunResult,
    RunStartedData,
    RunStreamEvent,
    TextDeltaData,
    ValidationPassedData,
    ValidationRetryData,
    ValidationStartedData,
)
from dagent.schemas import RunState


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def _gated(events: list[RunStreamEvent], *, validation_enabled: bool) -> list[RunStreamEvent]:
    async def _source() -> AsyncIterator[RunStreamEvent]:
        for event in events:
            yield event

    async def _collect() -> list[RunStreamEvent]:
        return [
            event
            async for event in gate_chat_display(_source(), validation_enabled=validation_enabled)
        ]

    return _run(_collect())


def _shape(events: list[RunStreamEvent]) -> list[str]:
    shaped = []
    for event in events:
        delta = getattr(event.data, "delta", None)
        shaped.append(f"{event.type}:{delta}" if delta is not None else event.type)
    return shaped


def _event(event_type: str, data: Any) -> RunStreamEvent:
    return RunStreamEvent(type=event_type, data=data, run_id="run_1")  # type: ignore[arg-type]


def _run_started(kind: str) -> RunStreamEvent:
    return _event("run.started", RunStartedData(kind=kind))  # type: ignore[arg-type]


def _response_started(response_id: str) -> RunStreamEvent:
    return _event("response.started", ResponseStartedData(response_id=response_id))


def _content(response_id: str, delta: str) -> RunStreamEvent:
    return _event("response.content.delta", TextDeltaData(delta=delta, response_id=response_id))


def _reasoning(response_id: str, delta: str) -> RunStreamEvent:
    return _event("response.reasoning.delta", TextDeltaData(delta=delta, response_id=response_id))


def _finished(response_id: str) -> RunStreamEvent:
    return _event("response.finished", ResponseFinishedData(response_id=response_id))


def _capability_started() -> RunStreamEvent:
    return _event(
        "capability.call.started",
        CapabilityCallStartedData(invocation_id="call_1", capability_id="tool.echo"),
    )


def _capability_completed() -> RunStreamEvent:
    return _event(
        "capability.call.completed",
        CapabilityCallCompletedData(invocation_id="call_1", capability_id="tool.echo"),
    )


def _validating() -> RunStreamEvent:
    return _event("validation.started", ValidationStartedData(message="Validating result quality..."))


def _validation_passed() -> RunStreamEvent:
    return _event("validation.passed", ValidationPassedData(summary="ok"))


def _validation_retry() -> RunStreamEvent:
    return _event("validation.retry", ValidationRetryData(summary="bad", reason="fix it"))


def _run_finished(*, kind: str = "tool", output_text: str = "") -> RunStreamEvent:
    state = RunState(run_id="run_1", kind=kind, status="completed")  # type: ignore[arg-type]
    return _event("run.finished", RunFinishedData(result=RunResult(state=state, output_text=output_text)))


def test_gate_holds_tool_final_answer_until_validation_passes() -> None:
    events = _gated(
        [
            _run_started("tool"),
            _response_started("r1"),
            _content("r1", "calling echo"),
            _finished("r1"),
            _capability_started(),
            _capability_completed(),
            _response_started("r2"),
            _reasoning("r2", "thinking"),
            _content("r2", "final answer"),
            _finished("r2"),
            _validating(),
            _validation_passed(),
            _run_finished(output_text="final answer"),
        ],
        validation_enabled=True,
    )

    assert _shape(events) == [
        "run.started",
        "response.started",
        "response.content.delta:calling echo",
        "response.finished",
        "capability.call.started",
        "capability.call.completed",
        "response.started",
        "response.reasoning.delta:thinking",
        "validation.started",
        "validation.passed",
        "response.content.delta:final answer",
        "response.finished",
        "run.finished",
    ]


def test_gate_discards_rejected_answer_once_retry_streams() -> None:
    events = _gated(
        [
            _run_started("tool"),
            _response_started("r1"),
            _content("r1", "rejected draft"),
            _finished("r1"),
            _validating(),
            _validation_retry(),
            _response_started("r2"),
            _content("r2", "improved answer"),
            _finished("r2"),
            _validating(),
            _validation_passed(),
            _run_finished(output_text="improved answer"),
        ],
        validation_enabled=True,
    )

    deltas = [getattr(event.data, "delta", "") for event in events if event.type == "response.content.delta"]
    assert deltas == ["improved answer"]
    types = [event.type for event in events]
    assert types.index("validation.passed") < types.index("response.content.delta")


def test_gate_releases_rejected_answer_when_retries_are_exhausted() -> None:
    events = _gated(
        [
            _run_started("tool"),
            _response_started("r1"),
            _content("r1", "unvalidated answer"),
            _finished("r1"),
            _validating(),
            _validation_retry(),
            _run_finished(output_text="unvalidated answer"),
        ],
        validation_enabled=True,
    )

    assert _shape(events) == [
        "run.started",
        "response.started",
        "validation.started",
        "validation.retry",
        "response.content.delta:unvalidated answer",
        "response.finished",
        "run.finished",
    ]


def test_gate_flushes_held_answer_at_run_finished_when_validation_is_skipped() -> None:
    events = _gated(
        [
            _run_started("tool"),
            _response_started("r1"),
            _content("r1", "answer"),
            _finished("r1"),
            _run_finished(output_text="answer"),
        ],
        validation_enabled=True,
    )

    assert _shape(events) == [
        "run.started",
        "response.started",
        "response.content.delta:answer",
        "response.finished",
        "run.finished",
    ]


def test_gate_is_passthrough_for_tool_runs_without_validation() -> None:
    source = [
        _run_started("tool"),
        _response_started("r1"),
        _content("r1", "calling echo"),
        _finished("r1"),
        _capability_started(),
        _capability_completed(),
        _response_started("r2"),
        _content("r2", "final answer"),
        _finished("r2"),
        _run_finished(output_text="final answer"),
    ]

    assert _gated(source, validation_enabled=False) == source


def test_gate_drops_dag_content_deltas_regardless_of_validation() -> None:
    source = [
        _run_started("dynamic_dag"),
        _response_started("r1"),
        _reasoning("r1", "planning"),
        _content("r1", "task: mock"),
        _finished("r1"),
        _capability_started(),
        _capability_completed(),
        _validating(),
        _validation_passed(),
        _run_finished(kind="dynamic_dag", output_text="dag answer"),
    ]
    expected = [event for event in source if event.type != "response.content.delta"]

    assert _gated(source, validation_enabled=True) == expected
    assert _gated(source, validation_enabled=False) == expected
