"""Run-scoped, concurrency-safe execution budget accounting."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from typing import Any, Iterator, Literal

from dagent.schemas import ExecutionLimits, ExecutionUsage


LimitName = Literal[
    "max_total_operations",
    "max_model_turns",
    "max_capability_calls",
]


class ExecutionLimitExceeded(RuntimeError):
    """Raised before an operation that would exceed a run-wide limit."""

    def __init__(self, limit_name: LimitName, limit: int) -> None:
        self.limit_name = limit_name
        self.limit = limit
        self.usage: ExecutionUsage | None = None
        self.checkpoint: Any | None = None
        super().__init__(f"Execution limit '{limit_name}' ({limit}) was exhausted.")

    def attach_checkpoint(self, checkpoint: Any, usage: ExecutionUsage) -> None:
        """Attach the terminal recovery data produced by ``Runner``."""

        self.checkpoint = checkpoint
        self.usage = usage


class ExecutionBudget:
    """Mutable counter shared by root, DAG, concurrent, and subagent work."""

    def __init__(
        self,
        limits: ExecutionLimits,
        usage: ExecutionUsage | None = None,
    ) -> None:
        self.limits = limits
        initial = usage or ExecutionUsage()
        self._total_operations = initial.total_operations
        self._model_turns = initial.model_turns
        self._capability_calls = initial.capability_calls
        self._lock = Lock()

    def reserve_model_turn(self) -> None:
        self._reserve(model_turns=1, capability_calls=0)

    def reserve_capability_call(self) -> None:
        self._reserve(model_turns=0, capability_calls=1)

    def snapshot(self) -> ExecutionUsage:
        with self._lock:
            return ExecutionUsage(
                total_operations=self._total_operations,
                model_turns=self._model_turns,
                capability_calls=self._capability_calls,
            )

    def _reserve(self, *, model_turns: int, capability_calls: int) -> None:
        with self._lock:
            next_total = self._total_operations + model_turns + capability_calls
            next_model_turns = self._model_turns + model_turns
            next_capability_calls = self._capability_calls + capability_calls
            self._ensure_within_limit(
                "max_total_operations",
                next_total,
                self.limits.max_total_operations,
            )
            self._ensure_within_limit(
                "max_model_turns",
                next_model_turns,
                self.limits.max_model_turns,
            )
            self._ensure_within_limit(
                "max_capability_calls",
                next_capability_calls,
                self.limits.max_capability_calls,
            )
            self._total_operations = next_total
            self._model_turns = next_model_turns
            self._capability_calls = next_capability_calls

    @staticmethod
    def _ensure_within_limit(
        limit_name: LimitName,
        next_value: int,
        limit: int | None,
    ) -> None:
        if limit is not None and next_value > limit:
            raise ExecutionLimitExceeded(limit_name, limit)


_CURRENT_EXECUTION_BUDGET: ContextVar[ExecutionBudget | None] = ContextVar(
    "dagent_execution_budget",
    default=None,
)


@contextmanager
def execution_budget_scope(budget: ExecutionBudget) -> Iterator[None]:
    token = _CURRENT_EXECUTION_BUDGET.set(budget)
    try:
        yield
    finally:
        _CURRENT_EXECUTION_BUDGET.reset(token)


def reserve_model_turn() -> None:
    budget = _CURRENT_EXECUTION_BUDGET.get()
    if budget is not None:
        budget.reserve_model_turn()


def reserve_capability_call() -> None:
    budget = _CURRENT_EXECUTION_BUDGET.get()
    if budget is not None:
        budget.reserve_capability_call()
