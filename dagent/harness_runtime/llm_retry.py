"""Retry helpers for transient LLM request failures."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar


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

_RETRYABLE_STATUS_CODES = {408, 409, 425, 429}
_RETRYABLE_EXCEPTION_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "ConnectError",
    "ConnectTimeout",
    "ConnectionTimeout",
    "InternalServerError",
    "NetworkError",
    "PoolTimeout",
    "RateLimitError",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "ServerTimeoutError",
    "ServiceUnavailableError",
    "TimeoutException",
    "WriteTimeout",
}


def is_transient_llm_error(exc: Exception) -> bool:
    """Return whether an LLM request failure is worth retrying."""

    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True

    status_code = _status_code(exc)
    if status_code is not None:
        return status_code in _RETRYABLE_STATUS_CODES or status_code >= 500

    return any(
        cls.__name__ in _RETRYABLE_EXCEPTION_NAMES
        for cls in type(exc).__mro__
    )


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
            if not is_transient_llm_error(exc):
                raise
            if should_retry is not None and not should_retry(exc):
                raise
            await sleep(policy.delay_for_retry(retry_index))
            retry_index += 1


def _status_code(exc: Exception) -> int | None:
    raw_status = getattr(exc, "status_code", None)
    if raw_status is None:
        response: Any = getattr(exc, "response", None)
        raw_status = getattr(response, "status_code", None)
    try:
        return int(raw_status)
    except (TypeError, ValueError):
        return None
