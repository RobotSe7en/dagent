"""Unified result type returned by all loop implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from dagent.harness_runtime.dag_executor import RunResult
from dagent.harness_runtime.task_record import PendingReview
from dagent.schemas import DAG


@dataclass(frozen=True)
class LoopResult:
    """Common contract between any loop (ToolAgentLoop, DAGAgentLoop) and the runtime.

    Every loop is responsible for populating this with its own results.
    The runtime never inspects internal loop state; it only reads LoopResult.
    """

    # What happened
    execution_context: str
    """Human-readable summary of what the loop did (tool calls, node results, etc.).
    Used by the validator and the summarizer.  Each loop formats this itself."""

    messages: list[dict[str, Any]]
    """Conversation messages produced during the loop, for runtime to record."""

    final_answer: str = ""
    """The answer produced by the loop, for the validator to assess.
    This is NOT the user-facing message; _summarize() still produces that."""

    events: list[dict[str, Any]] = field(default_factory=list)
    """UI-facing events produced by the loop (dag_created, dag_executed, etc.)."""

    # DAG-specific (None for tool mode)
    dag: DAG | None = None
    run_result: RunResult | None = None
    task_id: str | None = None

    # Flow control
    needs_human_review: bool = False
    pending_review: PendingReview | None = None

    # Status
    completed: bool = True
    """Whether the loop finished successfully."""
