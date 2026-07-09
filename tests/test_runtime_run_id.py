from __future__ import annotations

import pytest

import dagent
from dagent.providers.base import ChatResponse
from dagent.providers.mock import MockProvider
from dagent.schemas import DAGNode, DAGSpec, RunState, StartNodePayload


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
            messages=[{"role": "user", "content": "hi"}],
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
async def test_runner_stream_rejects_run_id_when_state_is_supplied(tmp_path) -> None:
    state = RunState(
        run_id="existing_run",
        kind="tool",
        status="completed",
    )
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([ChatResponse(content="done")]),
    )

    with pytest.raises(ValueError, match="run_id"):
        events = runner.stream(
            dagent.ToolAgent(profile="conversation"),
            messages=[{"role": "user", "content": "hi"}],
            state=state,
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
            messages=[{"role": "user", "content": "hi"}],
            run_id=bad_run_id,
            workspace_path=tmp_path / "explicit-workspace",
        )
        async for _event in events:
            pass

    runner.close()


@pytest.mark.asyncio
async def test_runner_rejects_reused_host_run_id_for_new_run_with_explicit_workspace_path(tmp_path) -> None:
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
        messages=[{"role": "user", "content": "first"}],
        run_id="duplicate_run",
        workspace_path=tmp_path / "workspace-one",
    )

    with pytest.raises(ValueError, match="already exists"):
        await runner.run(
            agent,
            messages=[{"role": "user", "content": "second"}],
            run_id="duplicate_run",
            workspace_path=tmp_path / "workspace-two",
        )

    runner.close()
