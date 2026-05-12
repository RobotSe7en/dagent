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
from dagent.schemas import DAG, TraceEvent


class MessageRequest(BaseModel):
    message: str = Field(min_length=1)
    mode: RuntimeMode = "auto"
    review_level: ReviewLevel = "fast"


class ResumeDagRequest(BaseModel):
    task_id: str = Field(min_length=1)
    dag: DAG
    review_level: ReviewLevel | None = None


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


@app.get("/settings/reviewer")
async def get_reviewer_status() -> dict[str, bool]:
    runtime = state.get_harness_runtime()
    return {"enabled": runtime.enable_reviewer}


@app.post("/settings/reviewer")
async def toggle_reviewer(payload: dict[str, bool]) -> dict[str, bool]:
    runtime = state.get_harness_runtime()
    runtime.enable_reviewer = payload.get("enabled", False)
    return {"enabled": runtime.enable_reviewer}


@app.post("/session/reset")
async def reset_session() -> dict[str, str]:
    state.harness_runtime = None
    return {"status": "ok"}


@app.post("/messages/stream")
async def message_stream(request: MessageRequest) -> StreamingResponse:
    async def events():
        yield _sse({"type": "status", "message": "agent_loop_started"})
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
        if result.run_result is not None:
            for trace in result.run_result.traces:
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
                "message_markdown": result.message_markdown,
            }
        )

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/messages/resume")
async def resume_message_stream(request: ResumeDagRequest) -> StreamingResponse:
    async def events():
        yield _sse({"type": "status", "message": "agent_loop_resumed"})
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
            state.get_harness_runtime().resume_dag(
                request.task_id,
                request.dag,
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
        if result.run_result is not None:
            for trace in result.run_result.traces:
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
                "message_markdown": result.message_markdown,
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
                record.model_dump(mode="json")
                for record in runtime.dag_agent_loop.dag_executor.trace_store.records_for_task(task_id)
            ],
        }

    raise HTTPException(status_code=404, detail="Task not found.")


def _trace_payload(trace: TraceEvent) -> dict[str, Any]:
    return trace.model_dump(mode="json")


def _review_payload(review) -> dict[str, Any]:
    return {
        "review_id": review.review_id,
        "kind": review.kind,
        "message": review.message,
        "dag": review.proposed_dag.model_dump(mode="json"),
        "payload": review.payload,
    }


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
