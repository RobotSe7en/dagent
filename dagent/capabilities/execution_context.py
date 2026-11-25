"""Context-local dispatch policy shared by tool adapters and the runtime."""

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator


_PARALLEL_DISPATCH: ContextVar[bool] = ContextVar("dagent_parallel_dispatch", default=False)


def is_parallel_dispatch() -> bool:
    return _PARALLEL_DISPATCH.get()


@contextmanager
def parallel_dispatch(enabled: bool) -> Iterator[None]:
    token = _PARALLEL_DISPATCH.set(enabled)
    try:
        yield
    finally:
        _PARALLEL_DISPATCH.reset(token)


def validate_concurrency(value: int, *, label: str = "max_tool_concurrency") -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer.")
    return value
