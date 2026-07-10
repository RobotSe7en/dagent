from __future__ import annotations

import asyncio

import pytest

import dagent
from dagent.providers.mock import MockProvider


@pytest.mark.asyncio
async def test_stream_cancellation_waits_for_sdk_task_cleanup(
    tmp_path,
    monkeypatch,
) -> None:
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([]),
    )
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleaned = asyncio.Event()

    async def blocking_run(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await allow_cleanup.wait()
            cleaned.set()

    monkeypatch.setattr(runner, "run", blocking_run)

    async def consume() -> None:
        async for _event in runner.stream(
            dagent.ToolAgent(profile="conversation"),
            messages=[{"role": "user", "content": "wait"}],
        ):
            pass

    consumer = asyncio.create_task(consume())
    await started.wait()
    consumer.cancel()

    await cleanup_started.wait()
    returned_before_cleanup = consumer.done()
    allow_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert returned_before_cleanup is False
    assert cleaned.is_set()
    runner.close()


def test_runner_close_is_idempotent_and_post_close_calls_fail(tmp_path) -> None:
    closed: list[str] = []
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([]),
    )
    runner.runtime.capability_catalog.add_shutdown_hook(
        lambda: closed.append("closed")
    )

    runner.close()
    runner.close()

    assert closed == ["closed"]
    with pytest.raises(RuntimeError, match="closed"):
        runner.validate_capability_refs([])
