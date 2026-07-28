from __future__ import annotations

import asyncio

import pytest

import dagent
from dagent.providers.base import ChatResponse
from dagent.providers.mock import MockProvider
from dagent.schemas import DAGNode, DAGSpec, StartNodePayload


@pytest.mark.asyncio
async def test_runner_cancel_stops_an_active_streamed_run(tmp_path) -> None:
    class BlockingProvider:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def chat(self, messages, tools=None, *, response_format=None):
            self.started.set()
            await asyncio.Event().wait()

    provider = BlockingProvider()
    runner = dagent.Runner(workspace=tmp_path, provider=provider)
    events = runner.stream(
        dagent.ToolAgent(profile="conversation"),
        input="wait",
        run_id="cancelled_run",
    )

    started_event = await anext(events)
    await provider.started.wait()

    assert started_event.type == "run.started"
    assert await runner.cancel("cancelled_run") is True
    with pytest.raises(asyncio.CancelledError):
        while True:
            await anext(events)
    assert await runner.cancel("cancelled_run") is False
    runner.close()


@pytest.mark.asyncio
async def test_runner_stream_uses_host_run_id_for_tool_agent(tmp_path) -> None:
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([ChatResponse(content="done")]),
    )

    events = [
        event
        async for event in runner.stream(
            dagent.ToolAgent(profile="conversation"),
            input="hi",
            run_id="enterprise_run_123",
        )
    ]

    assert events[0].type == "run.started"
    assert events[0].run_id == "enterprise_run_123"
    assert events[-1].type == "run.finished"
    assert events[-1].run_id == "enterprise_run_123"
    assert events[-1].data.result.state.run_id == "enterprise_run_123"
    runner.close()


@pytest.mark.asyncio
async def test_runner_stream_does_not_accept_runtime_state(tmp_path) -> None:
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([ChatResponse(content="done")]),
    )

    with pytest.raises(TypeError, match="unexpected keyword argument 'state'"):
        events = runner.stream(
            dagent.ToolAgent(profile="conversation"),
            input="hi",
            state=None,
            run_id="different_run",
        )
        async for _event in events:
            pass

    runner.close()


@pytest.mark.asyncio
async def test_runner_stream_uses_host_run_id_for_static_dag_spec(tmp_path) -> None:
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([]),
    )
    spec = DAGSpec(
        id="spec_1",
        name="Start only",
        nodes=[DAGNode(id="start", payload=StartNodePayload(type="start"))],
    )

    events = [
        event
        async for event in runner.stream(
            spec,
            run_id="static_run_123",
        )
    ]

    assert events[0].type == "run.started"
    assert events[0].run_id == "static_run_123"
    assert events[-1].type == "run.finished"
    assert events[-1].data.result.state.run_id == "static_run_123"
    assert events[-1].data.result.state.kind == "static_dag"
    runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_run_id", ["../escape", "/tmp/run", ".", "nested/path"])
async def test_runner_stream_rejects_unsafe_host_run_id_even_with_explicit_workspace_path(
    tmp_path,
    bad_run_id: str,
) -> None:
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([ChatResponse(content="done")]),
    )

    with pytest.raises(ValueError, match="run_id"):
        events = runner.stream(
            dagent.ToolAgent(profile="conversation"),
            input="hi",
            run_id=bad_run_id,
            workspace_path=tmp_path / "explicit-workspace",
        )
        async for _event in events:
            pass

    runner.close()


@pytest.mark.asyncio
async def test_runner_rejects_reused_host_run_id_for_new_run_with_explicit_workspace_path(
    tmp_path,
) -> None:
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([
            ChatResponse(content="first"),
            ChatResponse(content="second"),
        ]),
    )
    agent = dagent.ToolAgent(profile="conversation")

    await runner.run(
        agent,
        input="first",
        run_id="duplicate_run",
        workspace_path=tmp_path / "workspace-one",
    )

    with pytest.raises(ValueError, match="already exists"):
        await runner.run(
            agent,
            input="second",
            run_id="duplicate_run",
            workspace_path=tmp_path / "workspace-two",
        )

    runner.close()
