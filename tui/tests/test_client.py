from __future__ import annotations

import json

import httpx
import pytest

from dagent_tui.client import DagentApi, DagentApiError
from dagent_tui.models import Conversation


@pytest.mark.asyncio
async def test_navigation_loads_standalone_and_project_conversations() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/conversations":
            return httpx.Response(
                200,
                json={
                    "conversations": [
                        {"id": "conv-1", "title": "Standalone"},
                        {"id": "static-1", "title": "Static", "kind": "static_dag"},
                    ]
                },
            )
        if request.url.path == "/projects":
            return httpx.Response(200, json={"projects": [{"id": "proj-1", "name": "Demo"}]})
        if request.url.path == "/projects/proj-1/conversations":
            return httpx.Response(
                200,
                json={
                    "conversations": [
                        {"id": "conv-2", "title": "Project chat", "project_id": "proj-1"}
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    api = DagentApi("http://test", transport=httpx.MockTransport(handler))
    try:
        await api.health()
        navigation = await api.navigation()
    finally:
        await api.aclose()

    assert [item.title for item in navigation.standalone] == ["Standalone"]
    assert navigation.projects[0][0].name == "Demo"
    assert navigation.projects[0][1][0].project_id == "proj-1"


@pytest.mark.asyncio
async def test_create_dynamic_dag_conversation_creates_tui_orchestration_session() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path == "/conversations":
            return httpx.Response(
                200,
                json={
                    "conversation": {
                        "id": "conv-dag",
                        "title": "Plan",
                        "kind": "dynamic_dag",
                    }
                },
            )
        if request.url.path == "/orchestration-sessions":
            return httpx.Response(200, json={"session": {"id": "orch-1"}})
        raise AssertionError(f"Unexpected request: {request.url}")

    api = DagentApi("http://test", transport=httpx.MockTransport(handler))
    try:
        conversation = await api.create_conversation("Plan", kind="dynamic_dag")
    finally:
        await api.aclose()

    assert conversation.kind == "dynamic_dag"
    assert requests == [
        ("/conversations", {"title": "Plan", "kind": "dynamic_dag"}),
        (
            "/orchestration-sessions",
            {
                "conversation_id": "conv-dag",
                "project_id": None,
                "kind": "dynamic_dag",
                "ui_state": {"surface": "orchestration_workspace"},
            },
        ),
    ]


@pytest.mark.asyncio
async def test_stream_message_decodes_sse_and_sends_persisted_context() -> None:
    seen_payload: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/messages/stream":
            raise AssertionError(f"Unexpected request: {request.url}")
        seen_payload.update(json.loads(request.content))
        body = b"".join(
            [
                b'data: {"type":"run.started","data":{"kind":"tool"},"sequence":1,"run_id":"run-1"}\n\n',
                b': keepalive\n\n',
                b'data: {"type":"response.content.delta","data":{"delta":"hello","response_id":"r1"}}\n\n',
            ]
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    api = DagentApi("http://test/", transport=httpx.MockTransport(handler))
    conversation = Conversation(id="conv-1", title="Chat", project_id="proj-1")
    try:
        events = [
            event
            async for event in api.stream_message(
                conversation,
                "hello",
                target="auto",
                review_level="careful",
            )
        ]
    finally:
        await api.aclose()

    assert [event.type for event in events] == ["run.started", "response.content.delta"]
    assert events[0].run_id == "run-1"
    assert events[1].data["delta"] == "hello"
    assert seen_payload == {
        "input": "hello",
        "target": "auto",
        "review_level": "careful",
        "conversation_id": "conv-1",
        "project_id": "proj-1",
    }


@pytest.mark.asyncio
async def test_resume_review_uses_project_route_and_approved_dag() -> None:
    seen_path = ""
    seen_payload: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_path
        seen_path = request.url.path
        seen_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"type":"run.finished","data":{}}\n\n',
        )

    api = DagentApi("http://test", transport=httpx.MockTransport(handler))
    conversation = Conversation(id="conv-1", title="Chat", project_id="proj-1")
    try:
        events = [
            event
            async for event in api.resume_review(
                conversation,
                {"review_id": "review-1"},
                approved=True,
                feedback="ship it",
                review_level="careful",
                dag={"dag_id": "dag-1", "nodes": [], "edges": []},
            )
        ]
    finally:
        await api.aclose()

    assert events[0].type == "run.finished"
    assert seen_path == "/projects/proj-1/reviews/review-1/resume"
    assert seen_payload["approved"] is True
    assert seen_payload["feedback"] == "ship it"
    assert seen_payload["dag"] == {"dag_id": "dag-1", "nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_api_error_includes_fastapi_detail() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "Conversation is busy."})

    api = DagentApi("http://test", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(DagentApiError, match="Conversation is busy"):
            await api.health()
    finally:
        await api.aclose()
