"""FastAPI application exposing the dagent harness."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from dagent.config import load_config
from dagent.harness_runtime.dag_builder import validate_dag_spec
from dagent.harness_runtime.artifacts import ArtifactUpload
from dagent.harness_runtime import RuntimeMode
from dagent.capabilities.providers import template_capability_handler
from dagent.capabilities.skills import default_skill_roots, scan_skill_roots, skill_view_payload
from dagent.profiles import ProfileStore
from dagent.review import ReviewLevel
from dagent.runner import Runner
from dagent.schemas import DAG, DAGRun, DAGSpec
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


class DAGSpecRunRequest(BaseModel):
    workspace_root: str | None = None


class ApiState:
    def __init__(self) -> None:
        self.runner: Runner | None = None
        self.dag_specs: dict[str, DAGSpec] = {}
        self.dag_runs: dict[str, DAGRun] = {}
        self.dag_spec_artifact_uploads: dict[str, dict[str, list[ArtifactUpload]]] = {}
        self.profile_directory: str | None = None

    def get_runner(self) -> Runner:
        if self.runner is None:
            self.runner = Runner(workspace=".")
        return self.runner

    def close_runner(self) -> None:
        if self.runner is not None:
            self.runner.close()
        self.runner = None

    def get_runtime(self):
        runner = self.get_runner()
        return runner.runtime

    def get_profile_directory(self) -> str:
        if self.profile_directory is not None:
            return self.profile_directory
        try:
            return load_config().profiles.directory
        except Exception:
            return "profiles"

    def get_skill_roots(self) -> list[Path]:
        return default_skill_roots()


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
    runtime = state.get_runtime()
    return {"enabled": runtime.enable_validation}


@app.post("/settings/validation")
async def toggle_validation(payload: dict[str, bool]) -> dict[str, bool]:
    runtime = state.get_runtime()
    runtime.enable_validation = payload.get("enabled", False)
    return {"enabled": runtime.enable_validation}


@app.post("/session/reset")
async def reset_session() -> dict[str, str]:
    state.close_runner()
    state.dag_specs.clear()
    state.dag_runs.clear()
    state.dag_spec_artifact_uploads.clear()
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
    _prune_artifact_uploads(spec)
    return {"dag_spec": spec.model_dump(mode="json")}


@app.get("/dag-specs/{spec_id}")
async def get_dag_spec(spec_id: str) -> dict[str, Any]:
    spec = state.dag_specs.get(spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="DAGSpec not found.")
    return {"dag_spec": spec.model_dump(mode="json")}


@app.post("/dag-specs/{spec_id}/artifacts/{artifact_id}/upload")
async def upload_dag_spec_artifact(
    spec_id: str,
    artifact_id: str,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    spec = state.dag_specs.get(spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="DAGSpec not found.")
    if artifact_id not in spec.artifacts:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required.")

    uploads: list[ArtifactUpload] = []
    for file in files:
        content = await file.read()
        uploads.append(
            ArtifactUpload(
                filename=file.filename or "upload",
                content=content,
            )
        )
    state.dag_spec_artifact_uploads.setdefault(spec_id, {})[artifact_id] = uploads
    return {
        "artifact_id": artifact_id,
        "files": [upload.filename for upload in uploads],
    }


@app.post("/dag-specs/{spec_id}/run")
async def run_dag_spec(spec_id: str, request: DAGSpecRunRequest | None = None) -> dict[str, Any]:
    spec = state.dag_specs.get(spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="DAGSpec not found.")

    dag_run = await state.get_runtime().run_dag_spec(
        spec,
        workspace_root=_workspace_root_from_request(request),
        artifact_uploads=_artifact_uploads_for_spec(spec_id),
    )
    state.dag_runs[dag_run.run_id] = dag_run
    return {"dag_run": dag_run.model_dump(mode="json")}


@app.post("/dag-specs/{spec_id}/run/stream")
async def run_dag_spec_stream(spec_id: str, request: DAGSpecRunRequest | None = None) -> StreamingResponse:
    spec = state.dag_specs.get(spec_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="DAGSpec not found.")
    workspace_root = _workspace_root_from_request(request)

    async def events():
        yield _sse({"type": "status", "message": "dag_spec_run_started"})
        event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def on_token(content: str) -> None:
            event_queue.put_nowait({"type": "token", "content": content})

        def on_event(event: dict[str, Any]) -> None:
            event_queue.put_nowait(event)

        task = asyncio.create_task(
            state.get_runtime().run_dag_spec(
                spec,
                workspace_root=workspace_root,
                artifact_uploads=_artifact_uploads_for_spec(spec_id),
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
            dag_run = await task
        except Exception as exc:
            if not task.done():
                task.cancel()
            yield _sse({"type": "error", "message": str(exc)})
            return

        state.dag_runs[dag_run.run_id] = dag_run
        yield _sse(
            {
                "type": "done",
                "status": dag_run.status,
                "dag_run": dag_run.model_dump(mode="json"),
            }
        )

    return StreamingResponse(events(), media_type="text/event-stream")


def _workspace_root_from_request(request: DAGSpecRunRequest | None) -> str:
    if request is None or request.workspace_root is None or not request.workspace_root.strip():
        return ".dagent-runs"
    return request.workspace_root.strip()


def _artifact_uploads_for_spec(spec_id: str) -> dict[str, list[ArtifactUpload]]:
    return {
        artifact_id: list(uploads)
        for artifact_id, uploads in state.dag_spec_artifact_uploads.get(spec_id, {}).items()
    }


def _prune_artifact_uploads(spec: DAGSpec) -> None:
    uploads = state.dag_spec_artifact_uploads.get(spec.id)
    if not uploads:
        return
    for artifact_id in list(uploads):
        if artifact_id not in spec.artifacts:
            del uploads[artifact_id]


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
        "artifacts": {
            artifact_id: artifact_state.model_dump(mode="json")
            for artifact_id, artifact_state in dag_run.trace.artifacts.items()
        },
    }


@app.get("/capabilities")
async def list_capabilities(kind: str | None = None) -> dict[str, Any]:
    runtime = state.get_runtime()
    return {
        "capabilities": [
            definition.model_dump(mode="json")
            for definition in runtime.capability_catalog.list(kind=kind)  # type: ignore[arg-type]
        ]
    }


@app.post("/capabilities")
async def create_capability(definition: CapabilityDefinition) -> dict[str, Any]:
    runtime = state.get_runtime()
    _install_capability(runtime, definition)
    return {"capability": definition.model_dump(mode="json")}


@app.put("/capabilities/{capability_id}")
async def update_capability(capability_id: str, definition: CapabilityDefinition) -> dict[str, Any]:
    runtime = state.get_runtime()
    if capability_id != definition.id:
        raise HTTPException(status_code=400, detail="Capability id mismatch.")
    if runtime.capability_catalog.get(capability_id) is None:
        raise HTTPException(status_code=404, detail="Capability not found.")
    _replace_capability(runtime, definition)
    return {"capability": definition.model_dump(mode="json")}


@app.delete("/capabilities/{capability_id}")
async def delete_capability(capability_id: str) -> dict[str, str]:
    runtime = state.get_runtime()
    if runtime.capability_catalog.get(capability_id) is None:
        raise HTTPException(status_code=404, detail="Capability not found.")
    runtime.capability_catalog.delete(capability_id)
    runtime.refresh_toolsets()
    return {"status": "deleted"}


@app.post("/capabilities/{capability_id}/enable")
async def enable_capability(capability_id: str) -> dict[str, Any]:
    return _set_capability_enabled(capability_id, True)


@app.post("/capabilities/{capability_id}/disable")
async def disable_capability(capability_id: str) -> dict[str, Any]:
    return _set_capability_enabled(capability_id, False)


@app.get("/mcp/servers")
async def list_mcp_servers() -> dict[str, Any]:
    runtime = state.get_runtime()
    servers = sorted({
        definition.config.get("server")
        for definition in runtime.capability_catalog.list(kind="mcp")  # type: ignore[arg-type]
        if definition.config.get("server")
    })
    return {"servers": servers}


@app.get("/skills")
async def list_skills() -> dict[str, Any]:
    return {"skills": [skill.as_list_item() for skill in scan_skill_roots(state.get_skill_roots())]}


@app.get("/skills/{name:path}")
async def get_skill(name: str, file_path: str | None = None) -> dict[str, Any]:
    payload = skill_view_payload(name, state.get_skill_roots(), file_path=file_path)
    if payload.get("success") is False:
        status_code = 400 if payload.get("matches") else 404
        raise HTTPException(status_code=status_code, detail=payload)
    return payload


@app.get("/profiles")
async def list_profiles() -> dict[str, Any]:
    directory = Path(state.get_profile_directory())
    store = ProfileStore(directory)
    profiles: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    if not directory.exists():
        return {"profiles": [], "warnings": [{"name": str(directory), "error": "Profiles directory not found."}]}
    for profile_dir in sorted((path for path in directory.iterdir() if path.is_dir()), key=lambda path: path.name):
        if not (profile_dir / "profile.yaml").exists():
            continue
        try:
            profiles.append(store.load(profile_dir.name).model_dump(mode="json"))
        except Exception as exc:
            warnings.append({"name": profile_dir.name, "error": str(exc)})
    return {"profiles": profiles, "warnings": warnings}


@app.get("/sandbox/status")
async def sandbox_status() -> dict[str, Any]:
    runtime = state.get_runtime()
    return {
        "runner": "local-dev",
        "workspace_root": str(runtime.capability_executor.workspace_root),
        "container_ready": False,
    }


def _install_capability(runtime, definition: CapabilityDefinition) -> None:
    runtime.register_capability(definition, _handler_for_definition(definition))


def _replace_capability(runtime, definition: CapabilityDefinition) -> None:
    runtime.replace_capability(definition, _handler_for_definition(definition))


def _handler_for_definition(definition: CapabilityDefinition):
    if definition.kind != "tool":
        raise HTTPException(
            status_code=400,
            detail="Only tool capabilities can be created through this endpoint.",
        )
    return template_capability_handler(str(definition.config.get("template", "")))


def _set_capability_enabled(capability_id: str, enabled: bool) -> dict[str, Any]:
    runtime = state.get_runtime()
    definition = runtime.capability_catalog.get(capability_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Capability not found.")
    updated = runtime.capability_catalog.set_enabled(capability_id, enabled)
    runtime.refresh_toolsets()
    return {"capability": updated.model_dump(mode="json")}


def _sync_runtime_toolsets(runtime) -> None:
    runtime.refresh_toolsets()


@app.post("/capabilities/{capability_id}/test")
async def test_capability(capability_id: str, request: CapabilityTestRequest) -> dict[str, Any]:
    runtime = state.get_runtime()
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
    result = await runtime.capability_executor.execute(invocation)
    return {"result": result.model_dump(mode="json")}


@app.post("/messages/stream")
async def message_stream(request: MessageRequest) -> StreamingResponse:
    async def events():
        yield _sse({"type": "status", "message": "harness_runtime_started"})
        event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def on_token(content: str) -> None:
            event_queue.put_nowait({"type": "token", "content": content})

        def on_event(event: dict[str, Any]) -> None:
            event_queue.put_nowait(event)

        task = asyncio.create_task(
            state.get_runtime().handle_message(
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
        yield _sse(
            {
                "type": "done",
                "status": result.status,
                "task_id": result.task_id,
                "dag": result.dag.model_dump(mode="json") if result.dag else None,
                "pending_review": _review_payload(result.pending_review) if result.pending_review else None,
                "final_answer": result.final_answer,
                "trace": result.trace.model_dump(mode="json") if result.trace else None,
            }
        )

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/messages/resume")
async def resume_message_stream(request: ResumeReviewRequest) -> StreamingResponse:
    async def events():
        yield _sse({"type": "status", "message": "harness_runtime_resumed"})
        event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def on_token(content: str) -> None:
            event_queue.put_nowait({"type": "token", "content": content})

        def on_event(event: dict[str, Any]) -> None:
            event_queue.put_nowait(event)

        task = asyncio.create_task(
            state.get_runtime().resume_review(
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
        yield _sse(
            {
                "type": "done",
                "status": result.status,
                "task_id": result.task_id,
                "dag": result.dag.model_dump(mode="json") if result.dag else None,
                "pending_review": _review_payload(result.pending_review) if result.pending_review else None,
                "final_answer": result.final_answer,
                "trace": result.trace.model_dump(mode="json") if result.trace else None,
            }
        )

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/tasks/{task_id}/trace")
async def get_task_trace(task_id: str) -> dict[str, Any]:
    runtime = state.runner.runtime if state.runner is not None else None
    if runtime is not None and task_id in runtime.tasks:
        task = runtime.tasks[task_id]
        return {
            "task_id": task_id,
            "trace": task.trace.model_dump(mode="json") if task.trace else None,
        }

    raise HTTPException(status_code=404, detail="Task not found.")


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
