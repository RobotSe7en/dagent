"""Harness control plane for DAG creation, review, approval, and execution."""

from __future__ import annotations

from dataclasses import dataclass, field

from dagent.harness_runtime.dag_executor import DAGExecutor, RunResult
from dagent.harness_runtime.dag_validation import validate_dag
from dagent.harness_runtime.dag_creator import DagCreator
from dagent.schemas import DAG


@dataclass
class TaskRecord:
    task_id: str
    user_request: str
    dag: DAG
    runs: list[RunResult] = field(default_factory=list)


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
        result = await self.executor.execute(record.dag)
        record.runs.append(result)
        return result

    def _initial_status(self, dag: DAG) -> str:
        needs_review = any(node.risk in {"medium", "high"} for node in dag.nodes)
        if needs_review:
            return "review_required"
        return "approved" if self.auto_approve_low_risk else "draft"
