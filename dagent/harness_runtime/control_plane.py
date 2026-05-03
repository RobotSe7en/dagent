"""Harness control plane for DAG creation, review, approval, and execution."""

from __future__ import annotations

from dataclasses import dataclass, field

from dagent.harness_runtime.dag_executor import DAGExecutor, RunResult
from dagent.harness_runtime.dag_replanner import (
    DAGReplanner,
    NoOpDAGReplanner,
    ReplanContext,
    apply_replan_decision,
    replan_trace_event,
)
from dagent.harness_runtime.dag_validation import validate_dag
from dagent.harness_runtime.dag_creator import DagCreator
from dagent.schemas import Boundary, DAG, NodeExecutionRecord, PermissionRequest, TraceEvent


@dataclass
class TaskRecord:
    task_id: str
    user_request: str
    dag: DAG
    runs: list[RunResult] = field(default_factory=list)
    pending_permission_request: PermissionRequest | None = None
    node_results: dict = field(default_factory=dict)
    trace_records: list[NodeExecutionRecord] = field(default_factory=list)

    @property
    def node_execution_records(self) -> list[NodeExecutionRecord]:
        return self.trace_records


class ControlPlane:
    """Coordinates DagCreator -> review status -> DAGExecutor."""

    def __init__(
        self,
        *,
        dag_creator: DagCreator,
        executor: DAGExecutor,
        replanner: DAGReplanner | None = None,
        auto_approve_low_risk: bool = True,
        max_replans: int = 3,
    ) -> None:
        self.dag_creator = dag_creator
        self.executor = executor
        self.replanner = replanner or NoOpDAGReplanner()
        self.auto_approve_low_risk = auto_approve_low_risk
        self.max_replans = max_replans
        self.tasks: dict[str, TaskRecord] = {}

    async def create_task(self, user_request: str, *, task_id: str | None = None) -> TaskRecord:
        dag = await self.dag_creator.aplan(user_request, task_id=task_id)
        dag = self.prepare_dag_for_review(dag)
        record = TaskRecord(task_id=dag.task_id, user_request=user_request, dag=dag)
        self.tasks[dag.task_id] = record
        return record

    def prepare_dag_for_review(self, dag: DAG) -> DAG:
        prepared = self.executor.normalize(dag)
        validate_dag(prepared)
        self.executor.apply_risk_overrides(prepared)
        prepared.status = self._initial_status(prepared)
        return prepared

    def approve_dag(self, task_id: str) -> DAG:
        record = self.tasks[task_id]
        record.dag.status = "approved"
        return record.dag

    async def execute_task(self, task_id: str) -> RunResult:
        record = self.tasks[task_id]
        traces: list[TraceEvent] = []
        replan_count = 0
        try:
            while True:
                try:
                    result = await self.executor.execute_next_ready_layer(
                        record.dag,
                        initial_results=_completed_results(record.node_results),
                        record_dag_start=not traces,
                    )
                except Exception as exc:
                    traces.extend(self.executor.trace_recorder.events)
                    record.trace_records = self.executor.trace_store.records_for_task(record.task_id)
                    decision = await self._ask_replanner(
                        record,
                        last_error=str(exc),
                        failed_node_id=_latest_failed_node_id(record.trace_records),
                    )
                    if decision.action != "replace" or decision.dag is None or replan_count >= self.max_replans:
                        raise
                    record.dag = self._apply_replan(record, decision)
                    traces.append(
                        replan_trace_event(
                            dag_id=record.dag.dag_id,
                            decision=decision,
                            applied=True,
                        )
                    )
                    replan_count += 1
                    if record.dag.status == "review_required":
                        result = RunResult(
                            dag_id=record.dag.dag_id,
                            completed=False,
                            node_results=dict(record.node_results),
                            traces=traces,
                        )
                        break
                    continue

                traces.extend(result.traces)
                record.node_results.update(result.node_results)
                record.pending_permission_request = result.pending_permission_request
                record.trace_records = self.executor.trace_store.records_for_task(record.task_id)
                if result.pending_permission_request is not None or result.completed:
                    break

                decision = await self._ask_replanner(record)
                if decision.action == "replace" and decision.dag is not None:
                    record.dag = self._apply_replan(record, decision)
                    traces.append(
                        replan_trace_event(
                            dag_id=record.dag.dag_id,
                            decision=decision,
                            applied=True,
                        )
                    )
                    replan_count += 1
                    if replan_count > self.max_replans:
                        raise RuntimeError("Maximum DAG replans exceeded.")
                    if record.dag.status == "review_required":
                        result = RunResult(
                            dag_id=record.dag.dag_id,
                            completed=False,
                            node_results=dict(record.node_results),
                            traces=traces,
                        )
                        break

            result = RunResult(
                dag_id=record.dag.dag_id,
                completed=result.completed,
                node_results=result.node_results,
                traces=traces,
                pending_permission_request=result.pending_permission_request,
            )
        finally:
            record.trace_records = self.executor.trace_store.records_for_task(record.task_id)
        record.node_results.update(result.node_results)
        record.pending_permission_request = result.pending_permission_request
        if result.pending_permission_request is not None:
            record.dag.status = "paused_for_permission"
            _set_node_status(record.dag, result.pending_permission_request.node_id, "blocked_permission")
        elif result.completed:
            record.dag.status = "completed"
            for node in record.dag.nodes:
                node.status = "completed"
        elif record.dag.status == "review_required":
            pass
        else:
            record.dag.status = "failed"
        record.runs.append(result)
        return result

    async def _ask_replanner(
        self,
        record: TaskRecord,
        *,
        last_error: str | None = None,
        failed_node_id: str | None = None,
    ):
        return await self.replanner.replan(
            ReplanContext(
                task_id=record.task_id,
                user_request=record.user_request,
                dag=record.dag,
                node_results=_completed_results(record.node_results),
                trace_records=record.trace_records,
                last_error=last_error,
                failed_node_id=failed_node_id,
            )
        )

    def _apply_replan(self, record: TaskRecord, decision) -> DAG:
        replanned = apply_replan_decision(
            current=record.dag,
            decision=decision,
            node_results=_completed_results(record.node_results),
        )
        return self.prepare_dag_for_review(replanned)

    def approve_permission(
        self,
        task_id: str,
        *,
        boundary: Boundary | None = None,
    ) -> PermissionRequest:
        record = self.tasks[task_id]
        request = _require_pending_request(record)
        grant = boundary or request.requested_boundary
        node = _node_by_id(record.dag, request.node_id)
        node.boundary = grant
        node.status = "ready"
        request.status = "approved"
        record.pending_permission_request = None
        record.node_results.pop(node.id, None)
        record.dag.status = "approved"
        return request

    def deny_permission(self, task_id: str) -> PermissionRequest:
        record = self.tasks[task_id]
        request = _require_pending_request(record)
        request.status = "denied"
        record.pending_permission_request = None
        record.dag.status = "aborted"
        _set_node_status(record.dag, request.node_id, "failed")
        return request

    def _initial_status(self, dag: DAG) -> str:
        needs_review = any(node.risk in {"medium", "high"} for node in dag.nodes)
        if needs_review:
            return "review_required"
        return "approved" if self.auto_approve_low_risk else "draft"


def _completed_results(node_results: dict) -> dict:
    return {
        node_id: result
        for node_id, result in node_results.items()
        if getattr(result, "completed", False)
    }


def _node_by_id(dag: DAG, node_id: str):
    for node in dag.nodes:
        if node.id == node_id:
            return node
    raise KeyError(node_id)


def _set_node_status(dag: DAG, node_id: str, status: str) -> None:
    _node_by_id(dag, node_id).status = status


def _require_pending_request(record: TaskRecord) -> PermissionRequest:
    if record.pending_permission_request is None:
        raise KeyError("No pending permission request.")
    return record.pending_permission_request


def _latest_failed_node_id(records: list[NodeExecutionRecord]) -> str | None:
    for record in reversed(records):
        if record.status == "failed":
            return record.node_id
    return None
