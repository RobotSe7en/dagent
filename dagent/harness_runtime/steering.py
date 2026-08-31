"""Run-scoped cooperative steering for root tool-agent loops."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import Any, Literal
from uuid import uuid4

from dagent.steering import (
    RunNotSteerableError,
    SteerDiscardReason,
    SteerQueueFullError,
    SteerReceipt,
)


MAX_PENDING_STEERS = 32
RunSteeringPhase = Literal[
    "routing",
    "active",
    "validation",
    "awaiting_review",
    "finishing",
]
RunSteeringKind = Literal["tool", "dynamic_dag", "static_dag"]
SteeringEventHandler = Callable[[dict[str, Any]], None]
SteeringBoundHandler = Callable[["RunSteeringControl", str], None]


@dataclass(frozen=True)
class QueuedSteer:
    steer_id: str
    content: str


class RunSteeringControl:
    """Concurrency-safe mailbox and phase state for one root run."""

    def __init__(
        self,
        emit: SteeringEventHandler,
        on_bound: SteeringBoundHandler,
    ) -> None:
        self._emit = emit
        self._on_bound = on_bound
        self._lock = Lock()
        self._pending: deque[QueuedSteer] = deque()
        self._run_id: str | None = None
        self._kind: RunSteeringKind | None = None
        self._phase: RunSteeringPhase = "routing"
        self._closed = False

    @property
    def run_id(self) -> str | None:
        with self._lock:
            return self._run_id

    def bind(self, run_id: str, kind: RunSteeringKind) -> None:
        notify = False
        with self._lock:
            if self._closed:
                return
            if self._run_id is not None and self._run_id != run_id:
                raise RuntimeError("A steering control cannot be rebound to another run.")
            notify = self._run_id is None
            self._run_id = run_id
            self._kind = kind
            self._phase = "active" if kind == "tool" else "finishing"
        if notify:
            self._on_bound(self, run_id)

    def enqueue(self, content: str) -> SteerReceipt:
        with self._lock:
            run_id = self._run_id
            if self._closed or run_id is None:
                raise RunNotSteerableError("Run is no longer accepting steer messages.")
            if self._kind != "tool":
                raise RunNotSteerableError(
                    f"Active run '{run_id}' is not a tool-agent run."
                )
            if self._phase != "active":
                raise RunNotSteerableError(
                    f"Active run '{run_id}' is not steerable during phase '{self._phase}'."
                )
            if len(self._pending) >= MAX_PENDING_STEERS:
                raise SteerQueueFullError(
                    f"Active run '{run_id}' already has {MAX_PENDING_STEERS} queued steers."
                )
            queued = QueuedSteer(
                steer_id=f"steer_{uuid4().hex}",
                content=content,
            )
            self._pending.append(queued)
        self._emit({
            "type": "steer_queued",
            "run_id": run_id,
            "steer_id": queued.steer_id,
            "content": queued.content,
        })
        return SteerReceipt(run_id=run_id, steer_id=queued.steer_id)

    def drain(self, run_id: str) -> tuple[QueuedSteer, ...]:
        with self._lock:
            if not self._matches_active_phase(run_id):
                return ()
            pending = tuple(self._pending)
            self._pending.clear()
            return pending

    def drain_or_transition(
        self,
        run_id: str,
        phase: RunSteeringPhase,
    ) -> tuple[QueuedSteer, ...]:
        """Drain pending messages, or atomically leave the steerable phase."""

        with self._lock:
            if not self._matches_active_phase(run_id):
                return ()
            if self._pending:
                pending = tuple(self._pending)
                self._pending.clear()
                return pending
            self._phase = phase
            return ()

    def discard_and_transition(
        self,
        run_id: str,
        *,
        phase: RunSteeringPhase,
        reason: SteerDiscardReason,
    ) -> tuple[QueuedSteer, ...]:
        with self._lock:
            if not self._matches_active_phase(run_id):
                return ()
            pending = tuple(self._pending)
            self._pending.clear()
            self._phase = phase
        self._emit_discarded(run_id, pending, reason)
        return pending

    def set_phase(self, run_id: str, phase: RunSteeringPhase) -> None:
        with self._lock:
            if self._closed or self._run_id != run_id or self._kind != "tool":
                return
            self._phase = phase

    def emit_applied(self, run_id: str, steers: tuple[QueuedSteer, ...]) -> None:
        for steer in steers:
            self._emit({
                "type": "steer_applied",
                "run_id": run_id,
                "steer_id": steer.steer_id,
            })

    def emit_discarded(
        self,
        run_id: str,
        steers: tuple[QueuedSteer, ...],
        reason: SteerDiscardReason,
    ) -> None:
        self._emit_discarded(run_id, steers, reason)

    def close(self, reason: SteerDiscardReason | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._phase = "finishing"
            run_id = self._run_id
            pending = tuple(self._pending)
            self._pending.clear()
        if run_id is not None and pending and reason is not None:
            self._emit_discarded(run_id, pending, reason)

    def _matches_active_phase(self, run_id: str) -> bool:
        return (
            not self._closed
            and self._run_id == run_id
            and self._kind == "tool"
            and self._phase == "active"
        )

    def _emit_discarded(
        self,
        run_id: str,
        steers: tuple[QueuedSteer, ...],
        reason: SteerDiscardReason,
    ) -> None:
        for steer in steers:
            self._emit({
                "type": "steer_discarded",
                "run_id": run_id,
                "steer_id": steer.steer_id,
                "reason": reason,
            })


_RUN_STEERING_CONTROL: ContextVar[RunSteeringControl | None] = ContextVar(
    "dagent_run_steering_control",
    default=None,
)
_RUN_STEERING_SUSPENDED: ContextVar[bool] = ContextVar(
    "dagent_run_steering_suspended",
    default=False,
)


@contextmanager
def run_steering_context(control: RunSteeringControl) -> Iterator[None]:
    token = _RUN_STEERING_CONTROL.set(control)
    try:
        yield
    finally:
        _RUN_STEERING_CONTROL.reset(token)


def current_run_steering_control(run_id: str) -> RunSteeringControl | None:
    if _RUN_STEERING_SUSPENDED.get():
        return None
    control = _RUN_STEERING_CONTROL.get()
    if control is None or control.run_id != run_id:
        return None
    return control


@contextmanager
def suspend_run_steering() -> Iterator[None]:
    """Prevent nested agent loops from consuming their root run's mailbox."""

    token = _RUN_STEERING_SUSPENDED.set(True)
    try:
        yield
    finally:
        _RUN_STEERING_SUSPENDED.reset(token)


def bind_run_steering(run_id: str, kind: RunSteeringKind) -> None:
    control = _RUN_STEERING_CONTROL.get()
    if control is not None:
        control.bind(run_id, kind)


def set_run_steering_phase(run_id: str, phase: RunSteeringPhase) -> None:
    control = current_run_steering_control(run_id)
    if control is not None:
        control.set_phase(run_id, phase)


__all__ = [
    "MAX_PENDING_STEERS",
    "QueuedSteer",
    "RunSteeringControl",
    "RunSteeringKind",
    "RunSteeringPhase",
    "bind_run_steering",
    "current_run_steering_control",
    "run_steering_context",
    "set_run_steering_phase",
    "suspend_run_steering",
]
