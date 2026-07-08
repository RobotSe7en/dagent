"""Retry helpers for transient LLM request failures."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar


T = TypeVar("T")
LLMRetrySleep = Callable[[float], Awaitable[None]]
LLMRetryPredicate = Callable[[Exception], bool]


@dataclass(frozen=True)
class LLMRetryPolicy:
    max_retries: int = 5
    retry_delays_seconds: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)

    def delay_for_retry(self, retry_index: int) -> float:
        if not self.retry_delays_seconds:
            return 0.0
        if retry_index >= len(self.retry_delays_seconds):
            return max(0.0, self.retry_delays_seconds[-1])
        return max(0.0, self.retry_delays_seconds[retry_index])


DEFAULT_LLM_RETRY_POLICY = LLMRetryPolicy()


async def run_with_llm_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: LLMRetryPolicy = DEFAULT_LLM_RETRY_POLICY,
    sleep: LLMRetrySleep = asyncio.sleep,
    should_retry: LLMRetryPredicate | None = None,
) -> T:
    retry_index = 0
    while True:
        try:
            return await operation()
        except Exception as exc:
            if retry_index >= max(0, policy.max_retries):
                raise
            if should_retry is not None and not should_retry(exc):
                raise
            await sleep(policy.delay_for_retry(retry_index))
            retry_index += 1
