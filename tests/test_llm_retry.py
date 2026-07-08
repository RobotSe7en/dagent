import asyncio

import pytest

from dagent.harness_runtime.llm_retry import run_with_llm_retries


def run(coro):
    return asyncio.run(coro)


class ProviderStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"provider status {status_code}")
        self.status_code = status_code


def test_llm_retry_does_not_retry_provider_client_status_error() -> None:
    attempts = 0
    sleeps: list[float] = []

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        raise ProviderStatusError(400)

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    with pytest.raises(ProviderStatusError, match="provider status 400"):
        run(run_with_llm_retries(operation, sleep=record_sleep))

    assert attempts == 1
    assert sleeps == []


def test_llm_retry_retries_provider_rate_limit_status_error() -> None:
    attempts = 0
    sleeps: list[float] = []

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderStatusError(429)
        return "ok"

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    assert run(run_with_llm_retries(operation, sleep=record_sleep)) == "ok"
    assert attempts == 2
    assert sleeps == [1.0]
