"""FastAPI application exposing the dagent harness."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from dagent.factory import create_control_plane
from dagent.harness.control_plane import ControlPlane, TaskRecord
from dagent.harness.dag_executor import DAGExecutionError, RunResult
from dagent.schemas import DAG, TraceEvent


class CreateTaskRequest(BaseModel):
    message: str = Field(min_length=1)
    task_id: str | None = None


class UpdateDagRequest(BaseModel):
    dag: DAG


class ApiState:
    def __init__(self) -> None:
        self.control_plane: ControlPlane | None = None
        self.runs: dict[str, RunResult] = {}

    def get_control_plane(self) -> ControlPlane:
        if self.control_plane is None:
            self.control_plane = create_control_plane(workspace_root=".")
        return self.control_plane


state = ApiState()
app = FastAPI(title="dagent API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tasks")
async def create_task(request: CreateTaskRequest) -> dict[str, Any]:
    record = await state.get_control_plane().create_task(
        request.message,
        task_id=request.task_id,
    )
    return _task_payload(record)


@app.post("/tasks/stream")
async def create_task_stream(request: CreateTaskRequest) -> StreamingResponse:
    async def events():
        yield _sse({"type": "status", "message": "planner_started"})
        try:
            record = await state.get_control_plane().create_task(
                request.message,
                task_id=request.task_id,
            )
        except Exception as exc:
            yield _sse({"type": "error", "message": str(exc)})
            return

        yield _sse({"type": "dag", "dag": record.dag.model_dump(mode="json")})
        for chunk in _chunks(_planning_markdown(record), size=36):
            yield _sse({"type": "token", "content": chunk})
        yield _sse({"type": "done", **_task_payload(record)})

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/dags/{task_id}")
async def get_dag(task_id: str) -> dict[str, Any]:
    record = _get_task(task_id)
    return {"dag": record.dag.model_dump(mode="json")}


@app.put("/dags/{task_id}")
async def update_dag(task_id: str, request: UpdateDagRequest) -> dict[str, Any]:
    control_plane = state.get_control_plane()
    record = _get_task(task_id)
    if request.dag.task_id != task_id:
        raise HTTPException(status_code=400, detail="DAG task_id does not match URL task_id.")
    record.dag = control_plane.prepare_dag_for_review(request.dag)
    return {"dag": record.dag.model_dump(mode="json")}


@app.post("/dags/{task_id}/approve")
async def approve_dag(task_id: str) -> dict[str, Any]:
    try:
        dag = state.get_control_plane().approve_dag(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found.") from exc
    return {"dag": dag.model_dump(mode="json")}


@app.post("/dags/{task_id}/execute")
async def execute_dag(task_id: str) -> dict[str, Any]:
    record = _get_task(task_id)
    record.dag.status = "running"
    try:
        result = await state.get_control_plane().execute_task(task_id)
    except DAGExecutionError as exc:
        record.dag.status = "review_required"
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        record.dag.status = "failed"
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    record.dag.status = "completed" if result.completed else "failed"
    run_id = f"run_{uuid4().hex}"
    state.runs[run_id] = result
    return {
        "run_id": run_id,
        "dag": record.dag.model_dump(mode="json"),
        "result": _run_payload(result),
        "message_markdown": _run_markdown(result),
    }


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    result = state.runs.get(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return {"run_id": run_id, "result": _run_payload(result)}


def _get_task(task_id: str) -> TaskRecord:
    record = state.get_control_plane().tasks.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return record


def _task_payload(record: TaskRecord) -> dict[str, Any]:
    return {
        "task_id": record.task_id,
        "dag": record.dag.model_dump(mode="json"),
        "message_markdown": _planning_markdown(record),
    }


def _run_payload(result: RunResult) -> dict[str, Any]:
    return {
        "dag_id": result.dag_id,
        "completed": result.completed,
        "node_results": {
            node_id: asdict(node_result)
            for node_id, node_result in result.node_results.items()
        },
        "traces": [_trace_payload(trace) for trace in result.traces],
    }


def _trace_payload(trace: TraceEvent) -> dict[str, Any]:
    return trace.model_dump(mode="json")


def _planning_markdown(record: TaskRecord) -> str:
    medium_or_high = [
        node for node in record.dag.nodes
        if node.risk in {"medium", "high"}
    ]
    review_line = (
        f"- **Review required:** {len(medium_or_high)} node(s) need human approval."
        if medium_or_high
        else "- **Review required:** none, DAG is ready to execute."
    )
    return "\n".join(
        [
            "### DAG generated",
            f"- **Task:** `{record.task_id}`",
            f"- **Status:** `{record.dag.status}`",
            f"- **Nodes:** {len(record.dag.nodes)}",
            review_line,
        ]
    )


def _run_markdown(result: RunResult) -> str:
    lines = [
        "### DAG execution result",
        f"- **Status:** {'completed' if result.completed else 'failed'}",
        f"- **Nodes executed:** {len(result.node_results)}",
        "",
    ]
    for node_id, node_result in result.node_results.items():
        lines.extend(
            [
                f"#### `{node_id}`",
                node_result.final_response or "_No response produced._",
                "",
            ]
        )
    return "\n".join(lines).strip()


def _chunks(text: str, *, size: int) -> list[str]:
    return [text[index:index + size] for index in range(0, len(text), size)]


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
