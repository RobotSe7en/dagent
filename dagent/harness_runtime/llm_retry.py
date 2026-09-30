"""Retry helpers for transient LLM request failures."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from time import monotonic
from typing import Any, TypeVar

from dagent.providers.base import ChatResponse
from dagent.providers.model_io import ModelResponse
from dagent.providers.openai_compatible import ProviderRequestError, ProviderResponseError
from dagent.schemas.context import ModelCallAttempt


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

    if isinstance(exc, ProviderRequestError):
        return is_transient_llm_error(exc.cause)
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
    attempts: list[ModelCallAttempt] = []
    while True:
        started = monotonic()
        try:
            result = await operation()
        except Exception as exc:
            retry = (
                retry_index < max(0, policy.max_retries)
                and is_transient_llm_error(exc)
                and (should_retry is None or should_retry(exc))
            )
            cause = exc.cause if isinstance(exc, ProviderRequestError) else exc
            attempts.append(ModelCallAttempt(
                attempt=retry_index + 1, elapsed_seconds=monotonic() - started,
                exception_type=type(cause).__name__, http_status=_status_code(exc),
                retry_delay_seconds=policy.delay_for_retry(retry_index) if retry else None,
            ))
            if isinstance(exc, ProviderRequestError):
                exc.metadata = exc.metadata.model_copy(update={"attempts": tuple(attempts)})
                if exc.response is not None:
                    exc.response = replace(exc.response, metadata=exc.metadata)
            elif isinstance(exc, ProviderResponseError) and exc.response is not None and exc.response.metadata is not None:
                exc.response = replace(exc.response, metadata=exc.response.metadata.model_copy(update={"attempts": tuple(attempts)}))
            if not retry:
                raise
            await sleep(policy.delay_for_retry(retry_index))
            retry_index += 1
        else:
            if isinstance(result, (ChatResponse, ModelResponse)) and result.metadata is not None:
                previous = result.metadata.attempts
                attempts.append(ModelCallAttempt(
                    attempt=retry_index + 1, elapsed_seconds=monotonic() - started,
                    http_status=previous[-1].http_status if previous else None,
                ))
                result = replace(result, metadata=result.metadata.model_copy(update={"attempts": tuple(attempts)}))
            return result


def _status_code(exc: Exception) -> int | None:
    raw_status = getattr(exc, "status_code", None)
    if raw_status is None:
        response: Any = getattr(exc, "response", None)
        raw_status = getattr(response, "status_code", None)
        if raw_status is None and isinstance(response, ModelResponse) and response.metadata is not None and response.metadata.attempts:
            raw_status = response.metadata.attempts[-1].http_status
    try:
        return int(raw_status)
    except (TypeError, ValueError):
        return None
