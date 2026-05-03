"""Immutable raw node execution trace storage."""

from __future__ import annotations

from uuid import uuid4

from dagent.schemas import DAG, DAGNode, NodeExecutionRecord


class TraceStore:
    """Stores raw node execution records for planning and audit.

    This first implementation is in-memory. The public methods are deliberately
    small so a database-backed store can replace it later.
    """

    def __init__(self) -> None:
        self._records: list[NodeExecutionRecord] = []

    def add_node_record(
        self,
        *,
        dag: DAG,
        node: DAGNode,
        output: str = "",
        error: str | None = None,
        status: str,
        stop_reason: str,
        steps: int,
    ) -> NodeExecutionRecord:
        record = NodeExecutionRecord(
            record_id=f"node_record_{uuid4().hex}",
            task_id=dag.task_id,
            dag_id=dag.dag_id,
            dag_version=dag.version,
            node_id=node.id,
            node_title=node.title,
            node_goal=node.goal,
            node_kind=node.kind,
            tool=node.tool,
            args=dict(node.args),
            output=output,
            error=error,
            status=status,
            stop_reason=stop_reason,
            steps=steps,
        )
        self._records.append(record)
        return record

    def records_for_task(self, task_id: str) -> list[NodeExecutionRecord]:
        return [record for record in self._records if record.task_id == task_id]

    def records_for_dag(self, dag_id: str) -> list[NodeExecutionRecord]:
        return [record for record in self._records if record.dag_id == dag_id]

    def records_for_node(self, task_id: str, node_id: str) -> list[NodeExecutionRecord]:
        return [
            record
            for record in self.records_for_task(task_id)
            if record.node_id == node_id
        ]

    def all_records(self) -> list[NodeExecutionRecord]:
        return list(self._records)
