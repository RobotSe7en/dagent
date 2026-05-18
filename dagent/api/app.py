"""FastAPI application exposing the dagent harness."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from dagent.factory import create_harness_runtime
from dagent.harness_runtime.dag_builder import validate_dag_spec
from dagent.harness_runtime import (
    HarnessRuntime,
    RuntimeMode,
)
from dagent.harness_runtime.review_policy import ReviewLevel
from dagent.capabilities.providers import template_capability_handler
from dagent.schemas import DAG, DAGRun, DAGSpec, CapabilityExecutionRecord, TraceEvent
from dagent.schemas import CapabilityDefinition, CapabilityInvocation


class MessageRequest(BaseModel):
    message: str = Field(min_length=1)
    mode: RuntimeMode = "auto"
    review_level: ReviewLevel = "fast"


class ResumeReviewRequest(BaseModel):
    review_id: str = Field(min_length=1)
    dag: DAG | None = None
    approved: bool = True
    review_level: ReviewLevel | None = None


class CapabilityTestRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    boundary: dict[str, Any] | None = None


class ApiState:
    def __init__(self) -> None:
        self.harness_runtime: HarnessRuntime | None = None
        self.dag_specs: dict[str, DAGSpec] = {}
        self.dag_runs: dict[str, DAGRun] = {}

    def get_harness_runtime(self) -> HarnessRuntime:
        if self.harness_runtime is None:
            self.harness_runtime = create_harness_runtime(workspace_root=".")
        return self.harness_runtime


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


@app.get("/settings/validation")
async def get_validation_status() -> dict[str, bool]:
    runtime = state.get_harness_runtime()
    return {"enabled": runtime.enable_validation}


@app.post("/settings/validation")
async def toggle_validation(payload: dict[str, bool]) -> dict[str, bool]:
    runtime = state.get_harness_runtime()
    runtime.enable_validation = payload.get("enabled", False)
    return {"enabled": runtime.enable_validation}


@app.post("/session/reset")
async def reset_session() -> dict[str, str]:
    state.harness_runtime = None
    state.dag_specs.clear()
    state.dag_runs.clear()
    return {"status": "ok"}


@app.get("/dag-specs")
async def list_dag_specs() -> dict[str, Any]:
    return {
        "dag_specs": [
            spec.model_dump(mode="json")
            for spec in sorted(state.dag_specs.values(), key=lambda item: item.id)
        ]
    }


@app.post("/dag-specs")
async def create_dag_spec(spec: DAGSpec) -> dict[str, Any]:
    try:
        validate_dag_spec(spec)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state.dag_specs[spec.id] = spec.model_copy(deep=True)
    return {"dag_spec": spec.model_dump(mode="json")}


@app.get("/dag-specs/{spec_id}")
async def get_dag_spec(spec_id: str) -> dict[str, Any]:
    spec = state.dag_specs.get(spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="DAGSpec not found.")
    return {"dag_spec": spec.model_dump(mode="json")}


@app.post("/dag-specs/{spec_id}/run")
async def run_dag_spec(spec_id: str) -> dict[str, Any]:
    spec = state.dag_specs.get(spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="DAGSpec not found.")

    dag_run = await state.get_harness_runtime().run_dag_spec(spec)
    state.dag_runs[dag_run.run_id] = dag_run
    return {"dag_run": dag_run.model_dump(mode="json")}


@app.get("/dag-runs/{run_id}")
async def get_dag_run(run_id: str) -> dict[str, Any]:
    dag_run = state.dag_runs.get(run_id)
    if dag_run is None:
        raise HTTPException(status_code=404, detail="DAGRun not found.")
    return {"dag_run": dag_run.model_dump(mode="json")}


@app.get("/dag-runs/{run_id}/artifacts")
async def get_dag_run_artifacts(run_id: str) -> dict[str, Any]:
    dag_run = state.dag_runs.get(run_id)
    if dag_run is None:
        raise HTTPException(status_code=404, detail="DAGRun not found.")
    return {
        "run_id": run_id,
        "artifact_states": {
            artifact_id: artifact_state.model_dump(mode="json")
            for artifact_id, artifact_state in dag_run.artifact_states.items()
        },
    }


@app.get("/capabilities")
async def list_capabilities(kind: str | None = None) -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    return {
        "capabilities": [
            definition.model_dump(mode="json")
            for definition in runtime.capability_catalog.list(kind=kind)  # type: ignore[arg-type]
        ]
    }


@app.post("/capabilities")
async def create_capability(definition: CapabilityDefinition) -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    _install_capability(runtime, definition)
    return {"capability": definition.model_dump(mode="json")}


@app.put("/capabilities/{capability_id}")
async def update_capability(capability_id: str, definition: CapabilityDefinition) -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    if capability_id != definition.id:
        raise HTTPException(status_code=400, detail="Capability id mismatch.")
    if runtime.capability_catalog.get(capability_id) is None:
        raise HTTPException(status_code=404, detail="Capability not found.")
    _replace_capability(runtime, definition)
    return {"capability": definition.model_dump(mode="json")}


@app.delete("/capabilities/{capability_id}")
async def delete_capability(capability_id: str) -> dict[str, str]:
    runtime = state.get_harness_runtime()
    if runtime.capability_catalog.get(capability_id) is None:
        raise HTTPException(status_code=404, detail="Capability not found.")
    runtime.capability_catalog.delete(capability_id)
    return {"status": "deleted"}


@app.post("/capabilities/{capability_id}/enable")
async def enable_capability(capability_id: str) -> dict[str, Any]:
    return _set_capability_enabled(capability_id, True)


@app.post("/capabilities/{capability_id}/disable")
async def disable_capability(capability_id: str) -> dict[str, Any]:
    return _set_capability_enabled(capability_id, False)


@app.get("/mcp/servers")
async def list_mcp_servers() -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    servers = sorted({
        definition.config.get("server")
        for definition in runtime.capability_catalog.list(kind="mcp")  # type: ignore[arg-type]
        if definition.config.get("server")
    })
    return {"servers": servers}


@app.get("/skills")
async def list_skills() -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    return {
        "skills": [
            definition.model_dump(mode="json")
            for definition in runtime.capability_catalog.list(kind="skill")  # type: ignore[arg-type]
        ]
    }


@app.get("/sandbox/status")
async def sandbox_status() -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    return {
        "runner": "local-dev",
        "workspace_root": str(runtime.capability_executor.workspace_root),
        "container_ready": False,
    }


def _install_capability(runtime: HarnessRuntime, definition: CapabilityDefinition) -> None:
    runtime.capability_catalog.register(definition, _handler_for_definition(definition))


def _replace_capability(runtime: HarnessRuntime, definition: CapabilityDefinition) -> None:
    runtime.capability_catalog.replace(definition, _handler_for_definition(definition))


def _handler_for_definition(definition: CapabilityDefinition):
    if definition.kind != "custom_tool":
        raise HTTPException(
            status_code=400,
            detail="Only custom_tool capabilities can be created through this endpoint.",
        )
    return template_capability_handler(str(definition.config.get("template", "")))


def _set_capability_enabled(capability_id: str, enabled: bool) -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    definition = runtime.capability_catalog.get(capability_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Capability not found.")
    updated = runtime.capability_catalog.set_enabled(capability_id, enabled)
    return {"capability": updated.model_dump(mode="json")}


@app.post("/capabilities/{capability_id}/test")
async def test_capability(capability_id: str, request: CapabilityTestRequest) -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    definition = runtime.capability_catalog.get(capability_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Capability not found.")
    invocation = CapabilityInvocation(
        capability_id=capability_id,
        kind=definition.kind,
        arguments=request.arguments,
    )
    if request.boundary is not None:
        from dagent.schemas import Boundary

        invocation.boundary = Boundary.model_validate(request.boundary)
    result = runtime.capability_executor.execute(invocation)
    return {"result": result.model_dump(mode="json")}


@app.post("/messages/stream")
async def message_stream(request: MessageRequest) -> StreamingResponse:
    async def events():
        yield _sse({"type": "status", "message": "harness_runtime_started"})
        event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        emitted_trace_ids: set[str] = set()

        def on_token(content: str) -> None:
            event_queue.put_nowait({"type": "token", "content": content})

        def on_event(event: dict[str, Any]) -> None:
            if event.get("type") == "trace":
                event_id = event.get("event", {}).get("event_id")
                if isinstance(event_id, str):
                    emitted_trace_ids.add(event_id)
            event_queue.put_nowait(event)

        task = asyncio.create_task(
            state.get_harness_runtime().handle_message(
                request.message,
                mode=request.mode,
                review_level=request.review_level,
                on_token=on_token,
                on_event=on_event,
            )
        )
        try:
            while not task.done():
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                yield _sse(event)
            while not event_queue.empty():
                yield _sse(event_queue.get_nowait())
            result = await task
        except Exception as exc:
            if not task.done():
                task.cancel()
            yield _sse({"type": "error", "message": str(exc)})
            return

        if result.dag is not None:
            yield _sse({"type": "dag", "dag": result.dag.model_dump(mode="json")})
        if result.pending_review is not None:
            yield _sse({"type": "review", "review": _review_payload(result.pending_review)})
        if result.dag_run is not None:
            for trace in result.dag_run.traces:
                if trace.event_id in emitted_trace_ids:
                    continue
                emitted_trace_ids.add(trace.event_id)
                yield _sse({"type": "trace", "event": _trace_payload(trace)})

        yield _sse(
            {
                "type": "done",
                "status": result.status,
                "task_id": result.task_id,
                "dag": result.dag.model_dump(mode="json") if result.dag else None,
                "pending_review": _review_payload(result.pending_review) if result.pending_review else None,
                "final_answer": result.final_answer,
                "artifact_states": {
                    artifact_id: artifact_state.model_dump(mode="json")
                    for artifact_id, artifact_state in result.artifact_states.items()
                },
            }
        )

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/messages/resume")
async def resume_message_stream(request: ResumeReviewRequest) -> StreamingResponse:
    async def events():
        yield _sse({"type": "status", "message": "harness_runtime_resumed"})
        event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        emitted_trace_ids: set[str] = set()

        def on_token(content: str) -> None:
            event_queue.put_nowait({"type": "token", "content": content})

        def on_event(event: dict[str, Any]) -> None:
            if event.get("type") == "trace":
                event_id = event.get("event", {}).get("event_id")
                if isinstance(event_id, str):
                    emitted_trace_ids.add(event_id)
            event_queue.put_nowait(event)

        task = asyncio.create_task(
            state.get_harness_runtime().resume_review(
                request.review_id,
                dag=request.dag,
                approved=request.approved,
                review_level=request.review_level,
                on_token=on_token,
                on_event=on_event,
            )
        )
        try:
            while not task.done():
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                yield _sse(event)
            while not event_queue.empty():
                yield _sse(event_queue.get_nowait())
            result = await task
        except Exception as exc:
            if not task.done():
                task.cancel()
            yield _sse({"type": "error", "message": str(exc)})
            return

        if result is None:
            yield _sse({"type": "error", "message": "Review session not found."})
            return

        if result.dag is not None:
            yield _sse({"type": "dag", "dag": result.dag.model_dump(mode="json")})
        if result.pending_review is not None:
            yield _sse({"type": "review", "review": _review_payload(result.pending_review)})
        if result.dag_run is not None:
            for trace in result.dag_run.traces:
                if trace.event_id in emitted_trace_ids:
                    continue
                emitted_trace_ids.add(trace.event_id)
                yield _sse({"type": "trace", "event": _trace_payload(trace)})

        yield _sse(
            {
                "type": "done",
                "status": result.status,
                "task_id": result.task_id,
                "dag": result.dag.model_dump(mode="json") if result.dag else None,
                "pending_review": _review_payload(result.pending_review) if result.pending_review else None,
                "final_answer": result.final_answer,
                "artifact_states": {
                    artifact_id: artifact_state.model_dump(mode="json")
                    for artifact_id, artifact_state in result.artifact_states.items()
                },
            }
        )

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/tasks/{task_id}/trace")
async def get_task_trace(task_id: str) -> dict[str, Any]:
    if state.harness_runtime is not None and task_id in state.harness_runtime.tasks:
        runtime = state.harness_runtime
        task = runtime.tasks[task_id]
        return {
            "task_id": task_id,
            "records": [
                _execution_record_payload(record)
                for record in task.execution_records
            ],
        }

    raise HTTPException(status_code=404, detail="Task not found.")


def _trace_payload(trace: TraceEvent) -> dict[str, Any]:
    return trace.model_dump(mode="json")


def _execution_record_payload(record: CapabilityExecutionRecord) -> dict[str, Any]:
    payload = record.model_dump(mode="json")
    payload["capability"] = record.invocation.capability_id
    payload["args"] = record.invocation.arguments
    return payload


def _review_payload(review) -> dict[str, Any]:
    payload = {
        "review_id": review.review_id,
        "kind": review.kind,
        "message": review.message,
        "payload": review.payload,
    }
    if review.proposed_dag is not None:
        payload["dag"] = review.proposed_dag.model_dump(mode="json")
    if review.capability_call is not None:
        payload["capability_call"] = {
            "invocation_id": review.capability_call["invocation_id"],
            "capability_id": review.capability_call["capability_id"],
            "arguments": review.capability_call["arguments"],
        }
    return payload


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
