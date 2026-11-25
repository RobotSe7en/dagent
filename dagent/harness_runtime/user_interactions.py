"""Execution-scoped interaction matching, waiting, and cleanup."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from dagent.harness_runtime.steering import current_run_steering_control
from dagent.capabilities.execution_context import is_parallel_dispatch
from dagent.schemas import CapabilityInvocation
from dagent.schemas.user_interactions import (
    ConfirmParameters, ConfirmRequest, EditorParameters, EditorRequest,
    InputParameters, InputRequest, InteractionEndReason, SelectParameters,
    SelectRequest, UserInteractionAbandonment, UserInteractionAnswer,
    UserInteractionEnded, UserInteractionRequest, UserInteractionResponse,
)
from dagent.user_interactions import (
    UserInteractionBusy, UserInteractionCancelled, UserInteractionClient,
    UserInteractionDeliveryError, UserInteractionFatalError, UserInteractionOptions,
    UserInteractionResponseRejected, UserInteractionTimeout,
    UserInteractionUnavailable, UserInteractionUnsupported,
)

_CURRENT: ContextVar[InteractionControl | None] = ContextVar("dagent_interactions", default=None)
_CALL: ContextVar[_InvocationUI | None] = ContextVar("dagent_interaction_call", default=None)


@dataclass
class _Pending:
    request: UserInteractionRequest
    future: asyncio.Future[bool | str]
    deadline: float | None
    reason: InteractionEndReason = "invocation_ended"
    timer: asyncio.TimerHandle | None = None
    ended: bool = False


class InteractionControl:
    def __init__(self, options: UserInteractionOptions,
                 emit: Callable[[dict[str, Any]], None] | None,
                 on_bound: Callable[[str], None]) -> None:
        self.options = options
        self.emit = emit
        self.on_bound = on_bound
        self.run_id: str | None = None
        self.pending: _Pending | None = None
        self.failure: UserInteractionFatalError | None = None
        self.closed = False

    def bind(self, run_id: str, kind: str) -> None:
        if kind != "tool":
            raise UserInteractionUnsupported("User interactions support root ToolLoop only.")
        if self.run_id is None:
            self.run_id = run_id
            self.on_bound(run_id)
        elif self.run_id != run_id:
            raise UserInteractionUnsupported("Interaction receiver cannot be shared with another run.")

    def check(self) -> None:
        if self.failure is not None:
            raise self.failure

    def _notify(self, callback: Callable | None, value: object) -> None:
        if callback is None:
            return
        result = callback(value)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("Interaction notifications must synchronously enqueue and return.")

    def _event(self, event_type: str, data: object) -> None:
        if self.emit is not None:
            self.emit({"type": event_type, "run_id": self.run_id, "data": data})

    def _end(self, pending: _Pending) -> None:
        if pending.ended:
            return
        pending.ended = True
        if pending.timer is not None:
            pending.timer.cancel()
        if not pending.future.done():
            pending.future.cancel()
        elif not pending.future.cancelled():
            pending.future.exception()
        if self.pending is pending:
            self.pending = None
        request = pending.request
        ended = UserInteractionEnded(
            **request.model_dump(include={"protocol_version", "interaction_id", "run_id",
                                          "invocation_id", "assistant_message_id", "capability_id"}),
            reason=pending.reason,
            invalidated=pending.reason not in {"submitted", "cancelled"},
        )
        steering = current_run_steering_control(request.run_id)
        if steering is not None:
            # Invalidating only the await need not terminate the tool or run.
            steering.set_phase(request.run_id, "finishing" if self.failure or self.closed else "active")
        for callback, payload in (
            (self.emit, {"type": "user_interaction_ended", "run_id": self.run_id, "data": ended}),
            (self.options.on_end, ended),
        ):
            try:
                self._notify(callback, payload)
            except Exception as exc:
                if self.failure is None and not ended.invalidated:
                    self.failure = UserInteractionDeliveryError(str(exc))
        if steering is not None and self.failure is not None:
            steering.set_phase(request.run_id, "finishing")

    async def ask(self, ui: _InvocationUI, parameters: object) -> bool | str:
        if self.closed or not ui.active or _CALL.get() is not ui or _CURRENT.get() is not self:
            raise UserInteractionUnsupported("This invocation no longer has an active interaction receiver.")
        self.check()
        if self.pending is not None:
            raise UserInteractionBusy("This run already has a pending user interaction.")
        request_type = {
            ConfirmParameters: ConfirmRequest, SelectParameters: SelectRequest,
            InputParameters: InputRequest, EditorParameters: EditorRequest,
        }[type(parameters)]
        request = request_type(
            interaction_id=f"interaction_{uuid4().hex}", run_id=self.run_id,
            invocation_id=ui.invocation.invocation_id,
            assistant_message_id=ui.assistant_message_id,
            capability_id=ui.invocation.capability_id, parameters=parameters,
        )
        loop = asyncio.get_running_loop()
        timeout = self.options.timeout_seconds
        pending = _Pending(request, loop.create_future(), None if timeout is None else loop.time() + timeout)
        self.pending = pending
        steering = current_run_steering_control(request.run_id)
        if steering is not None:
            steering.set_phase(request.run_id, "awaiting_user_interaction")
        if timeout is not None:
            pending.timer = loop.call_at(pending.deadline, self._expire, pending)
        try:
            try:
                self._event("user_interaction_requested", request)
                self._notify(self.options.on_request, request)
            except Exception as exc:
                pending.reason = "delivery_error"
                self.failure = UserInteractionDeliveryError(str(exc))
                raise self.failure from exc
            return await pending.future
        except asyncio.CancelledError:
            if pending.reason == "invocation_ended":
                pending.reason = "run_cancelled"
            raise
        finally:
            self._end(pending)
            # A notification failure after a valid answer is still fatal.
            if self.failure is not None:
                raise self.failure

    def _expire(self, pending: _Pending) -> None:
        if self.pending is pending and not pending.future.done():
            self._fail(pending, "timeout", UserInteractionTimeout("User interaction timed out."))

    def _fail(self, pending: _Pending, reason: InteractionEndReason, error: UserInteractionFatalError) -> None:
        pending.reason = reason
        self.failure = error
        pending.future.set_exception(error)

    def match(self, run_id: str, invocation_id: str, interaction_id: str) -> _Pending:
        pending = self.pending
        if self.closed or pending is None or pending.future.done():
            raise UserInteractionResponseRejected("Interaction is no longer pending.", code="not_pending")
        request = pending.request
        if (run_id, invocation_id, interaction_id) != (request.run_id, request.invocation_id, request.interaction_id):
            raise UserInteractionResponseRejected("Response identity does not match the pending interaction.", code="identity_mismatch")
        if pending.deadline is not None and asyncio.get_running_loop().time() >= pending.deadline:
            self._expire(pending)
            raise UserInteractionResponseRejected("Interaction deadline expired.", code="not_pending")
        return pending

    def respond(self, response: UserInteractionResponse) -> None:
        pending = self.match(response.run_id, response.invocation_id, response.interaction_id)
        if isinstance(response, UserInteractionAbandonment):
            pending.reason = "cancelled"
            pending.future.set_exception(UserInteractionCancelled("The user abandoned this interaction."))
            return
        if not isinstance(response, UserInteractionAnswer):
            raise UserInteractionResponseRejected("Unsupported interaction response.", code="invalid_value")
        value = response.value
        request = pending.request
        valid = type(value) is bool if request.type == "confirm" else type(value) is str
        if request.type == "select":
            valid = valid and value in {option.id for option in request.parameters.options}
        if not valid:
            raise UserInteractionResponseRejected("Response value does not match the request.", code="invalid_value")
        pending.reason = "submitted"
        pending.future.set_result(value)

    def abort(self, run_id: str, invocation_id: str, interaction_id: str, reason: str, message: str) -> None:
        pending = self.match(run_id, invocation_id, interaction_id)
        if reason == "timeout":
            self._fail(pending, "timeout", UserInteractionTimeout(message or "User interaction timed out."))
        elif reason == "unavailable":
            self._fail(pending, "unavailable", UserInteractionUnavailable(message or "User interaction is unavailable."))
        else:
            raise ValueError("reason must be 'timeout' or 'unavailable'.")

    def close(self, reason: InteractionEndReason = "invocation_ended") -> None:
        self.closed = True
        if self.pending is not None:
            if self.pending.reason == "invocation_ended":
                self.pending.reason = reason
            self._end(self.pending)


class _InvocationUI(UserInteractionClient):
    def __init__(self, control: InteractionControl, invocation: CapabilityInvocation, assistant_message_id: str) -> None:
        self.control = control
        self.invocation = invocation
        self.assistant_message_id = assistant_message_id
        self.active = True

    async def _request(self, parameters: ConfirmParameters | SelectParameters | InputParameters | EditorParameters) -> bool | str:
        return await self.control.ask(self, parameters)


class _ParallelInvocationUI(UserInteractionClient):
    async def _request(self, parameters: ConfirmParameters | SelectParameters | InputParameters | EditorParameters) -> bool | str:
        raise UserInteractionUnsupported("Parallel-safe tools cannot request user interaction; declare the tool exclusive.")


@contextmanager
def interaction_scope(control: InteractionControl | None) -> Iterator[None]:
    token = _CURRENT.set(control)
    call_token = _CALL.set(None)
    try:
        yield
    finally:
        _CALL.reset(call_token)
        _CURRENT.reset(token)


def bind_interactions(run_id: str, kind: str) -> None:
    control = _CURRENT.get()
    if control is not None:
        control.bind(run_id, kind)


@contextmanager
def invocation_interactions(run_id: str, invocation: CapabilityInvocation,
                            assistant_message_id: str) -> Iterator[UserInteractionClient]:
    if is_parallel_dispatch():
        # A parallel-safe tool promises not to open a run-owned interaction.
        # Do not create a UI scope whose cleanup could close a sibling's wait.
        yield _ParallelInvocationUI()
        return
    control = _CURRENT.get()
    ui = (_InvocationUI(control, invocation, assistant_message_id)
          if control is not None and control.run_id == run_id
          and current_run_steering_control(run_id) is not None else None)
    token = _CALL.set(ui)
    try:
        yield ui or UserInteractionClient()
        if ui is not None:
            control.check()
    finally:
        if ui is not None:
            ui.active = False
            if control.pending is not None:
                control._end(control.pending)
        _CALL.reset(token)
        if ui is not None:
            control.check()
