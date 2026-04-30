"""Harness control plane for DAG creation, review, approval, and execution."""

from __future__ import annotations

from dataclasses import dataclass, field

from dagent.harness_runtime.dag_executor import DAGExecutor, RunResult
from dagent.harness_runtime.dag_validation import validate_dag
from dagent.harness_runtime.dag_creator import DagCreator
from dagent.schemas import Boundary, DAG, PermissionRequest


@dataclass
class TaskRecord:
    task_id: str
    user_request: str
    dag: DAG
    runs: list[RunResult] = field(default_factory=list)
    pending_permission_request: PermissionRequest | None = None
    node_results: dict = field(default_factory=dict)


class ControlPlane:
    """Coordinates DagCreator -> review status -> DAGExecutor."""

    def __init__(
        self,
        *,
        dag_creator: DagCreator,
        executor: DAGExecutor,
        auto_approve_low_risk: bool = True,
    ) -> None:
        self.dag_creator = dag_creator
        self.executor = executor
        self.auto_approve_low_risk = auto_approve_low_risk
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
        result = await self.executor.execute(
            record.dag,
            initial_results=_completed_results(record.node_results),
        )
        record.node_results.update(result.node_results)
        record.pending_permission_request = result.pending_permission_request
        if result.pending_permission_request is not None:
            record.dag.status = "paused_for_permission"
            _set_node_status(record.dag, result.pending_permission_request.node_id, "blocked_permission")
        elif result.completed:
            record.dag.status = "completed"
            for node in record.dag.nodes:
                node.status = "completed"
        else:
            record.dag.status = "failed"
        record.runs.append(result)
        return result

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
