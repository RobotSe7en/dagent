"""Unified result type returned by all loop implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from dagent.harness_runtime.dag_executor import DAGRunResult
from dagent.harness_runtime.task_record import PendingReview
from dagent.schemas import DAG, ToolInvocation

LoopStatus = Literal["completed", "awaiting_review", "failed"]


@dataclass(frozen=True)
class LoopOutcome:
    """Common contract between any loop (ToolAgentLoop, DAGAgentLoop) and the runtime.

    Every loop is responsible for populating this with its own results.
    The runtime never inspects internal loop state; it only reads LoopOutcome.
    """

    status: LoopStatus
    """High-level loop state consumed by runtime orchestration."""

    execution_context: str = ""
    """Human-readable summary of what the loop did (tool calls, node results, etc.).
    Used by the validator and deterministic fallback output. Each loop formats this itself."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    """Conversation messages produced during the loop, for runtime to record."""

    final_answer: str = ""
    """The user-facing answer produced by the loop and checked by validation."""

    events: list[dict[str, Any]] = field(default_factory=list)
    """UI-facing events produced by the loop (dag_created, dag_executed, etc.)."""

    invocations: list[ToolInvocation] = field(default_factory=list)
    """Tool invocations produced by the loop, independent of execution shape."""

    # DAG-specific (None for tool mode)
    dag: DAG | None = None
    dag_run: DAGRunResult | None = None
    task_id: str | None = None

    pending_review: PendingReview | None = None
