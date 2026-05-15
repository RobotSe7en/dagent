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
from dagent.harness_runtime import (
    HarnessRuntime,
    RuntimeMode,
)
from dagent.harness_runtime.review_policy import ReviewLevel
from dagent.schemas import DAG, ToolExecutionRecord, TraceEvent
from dagent.schemas import RunnableDefinition, RunnableInvocation, RunnableResult


class MessageRequest(BaseModel):
    message: str = Field(min_length=1)
    mode: RuntimeMode = "auto"
    review_level: ReviewLevel = "fast"


class ResumeReviewRequest(BaseModel):
    review_id: str = Field(min_length=1)
    dag: DAG | None = None
    approved: bool = True
    review_level: ReviewLevel | None = None


class RunnableTestRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    boundary: dict[str, Any] | None = None


class ApiState:
    def __init__(self) -> None:
        self.harness_runtime: HarnessRuntime | None = None

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
    return {"status": "ok"}


@app.get("/runnables")
async def list_runnables(kind: str | None = None) -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    return {
        "runnables": [
            definition.model_dump(mode="json")
            for definition in runtime.runnable_registry.list(kind=kind)  # type: ignore[arg-type]
        ]
    }


@app.post("/runnables")
async def create_runnable(definition: RunnableDefinition) -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    _install_runnable(runtime, definition)
    return {"runnable": definition.model_dump(mode="json")}


@app.put("/runnables/{runnable_id}")
async def update_runnable(runnable_id: str, definition: RunnableDefinition) -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    if runnable_id != definition.id:
        raise HTTPException(status_code=400, detail="Runnable id mismatch.")
    if runtime.runnable_registry.get(runnable_id) is None:
        raise HTTPException(status_code=404, detail="Runnable not found.")
    runtime.runnable_registry.update(definition)
    return {"runnable": definition.model_dump(mode="json")}


@app.delete("/runnables/{runnable_id}")
async def delete_runnable(runnable_id: str) -> dict[str, str]:
    runtime = state.get_harness_runtime()
    if runtime.runnable_registry.get(runnable_id) is None:
        raise HTTPException(status_code=404, detail="Runnable not found.")
    runtime.runnable_registry.delete(runnable_id)
    return {"status": "deleted"}


@app.post("/runnables/{runnable_id}/enable")
async def enable_runnable(runnable_id: str) -> dict[str, Any]:
    return _set_runnable_enabled(runnable_id, True)


@app.post("/runnables/{runnable_id}/disable")
async def disable_runnable(runnable_id: str) -> dict[str, Any]:
    return _set_runnable_enabled(runnable_id, False)


@app.get("/mcp/servers")
async def list_mcp_servers() -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    servers = sorted({
        definition.config.get("server")
        for definition in runtime.runnable_registry.list(kind="mcp")  # type: ignore[arg-type]
        if definition.config.get("server")
    })
    return {"servers": servers}


@app.get("/skills")
async def list_skills() -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    return {
        "skills": [
            definition.model_dump(mode="json")
            for definition in runtime.runnable_registry.list(kind="skill")  # type: ignore[arg-type]
        ]
    }


@app.get("/sandbox/status")
async def sandbox_status() -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    return {
        "runner": "local-dev",
        "workspace_root": str(runtime.runnable_executor.workspace_root),
        "container_ready": False,
    }


def _install_runnable(runtime: HarnessRuntime, definition: RunnableDefinition) -> None:
    runtime.runnable_registry.register(definition)
    if definition.kind == "custom_tool":
        template = str(definition.config.get("template", ""))

        def handler(invocation: RunnableInvocation) -> RunnableResult:
            try:
                content = template.format(**invocation.arguments) if template else ""
            except Exception as exc:
                return RunnableResult(
                    invocation_id=invocation.invocation_id,
                    runnable_id=invocation.runnable_id,
                    kind=invocation.kind,
                    status="failed",
                    error=str(exc),
                    stop_reason=type(exc).__name__,
                )
            return RunnableResult(
                invocation_id=invocation.invocation_id,
                runnable_id=invocation.runnable_id,
                kind=invocation.kind,
                status="completed",
                content=content,
            )

        runtime.runnable_executor.register_handler(definition.id, handler)


def _set_runnable_enabled(runnable_id: str, enabled: bool) -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    definition = runtime.runnable_registry.get(runnable_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Runnable not found.")
    updated = definition.model_copy(update={"enabled": enabled})
    runtime.runnable_registry.update(updated)
    return {"runnable": updated.model_dump(mode="json")}


@app.post("/runnables/{runnable_id}/test")
async def test_runnable(runnable_id: str, request: RunnableTestRequest) -> dict[str, Any]:
    runtime = state.get_harness_runtime()
    definition = runtime.runnable_registry.get(runnable_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Runnable not found.")
    invocation = RunnableInvocation(
        runnable_id=runnable_id,
        kind=definition.kind,
        arguments=request.arguments,
    )
    if request.boundary is not None:
        from dagent.schemas import Boundary

        invocation.boundary = Boundary.model_validate(request.boundary)
    result = runtime.runnable_executor.execute(invocation)
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
            }
        )

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/tasks/{task_id}/trace")
async def get_task_trace(task_id: str) -> dict[str, Any]:
    if state.harness_runtime is not None and task_id in state.harness_runtime.tasks:
        runtime = state.harness_runtime
        return {
            "task_id": task_id,
            "records": [
                _execution_record_payload(record)
                for record in runtime.dag_agent.loop.dag_executor.execution_store.records_for_task(task_id)
            ],
        }

    raise HTTPException(status_code=404, detail="Task not found.")


def _trace_payload(trace: TraceEvent) -> dict[str, Any]:
    return trace.model_dump(mode="json")


def _execution_record_payload(record: ToolExecutionRecord) -> dict[str, Any]:
    payload = record.model_dump(mode="json")
    payload["tool"] = record.invocation.runnable_id
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
    if review.tool_call is not None:
        payload["tool_call"] = {
            "tool_call_id": review.tool_call["tool_call_id"],
            "name": review.tool_call["name"],
            "arguments": review.tool_call["arguments"],
        }
    return payload


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
