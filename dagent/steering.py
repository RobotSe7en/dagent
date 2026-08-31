"""Public helpers for steering active tool-agent runs."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict
from pydantic.dataclasses import dataclass


SteerStatus = Literal["queued"]
SteerDiscardReason = Literal[
    "step_limit_exhausted",
    "run_cancelled",
    "run_failed",
    "runner_closed",
]


@dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class SteerReceipt:
    """Acknowledgement that one steer message was queued for an active run."""

    run_id: str
    steer_id: str
    status: SteerStatus = "queued"


class SteerError(RuntimeError):
    """Base error for rejected steer submissions."""


class RunNotActiveError(SteerError):
    """Raised when a run has no active in-process execution."""


class RunNotSteerableError(SteerError):
    """Raised when an active run is not in a steerable tool-loop phase."""


class SteerQueueFullError(SteerError):
    """Raised when an active run already has the maximum queued steers."""


__all__ = [
    "RunNotActiveError",
    "RunNotSteerableError",
    "SteerDiscardReason",
    "SteerError",
    "SteerQueueFullError",
    "SteerReceipt",
    "SteerStatus",
]
