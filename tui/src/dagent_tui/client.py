"""Async HTTP/SSE client for the existing dagent API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx

from dagent_tui.models import (
    Conversation,
    ConversationMessage,
    Navigation,
    Project,
    ReviewLevel,
    RunTarget,
    StreamEnvelope,
)


class DagentApiError(RuntimeError):
    """An API request failed with a user-displayable message."""


class DagentApi:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized_url = base_url.rstrip("/")
        if not normalized_url:
            raise ValueError("API URL cannot be empty.")
        self.base_url = normalized_url
        self._client = httpx.AsyncClient(
            base_url=normalized_url,
            transport=transport,
            follow_redirects=True,
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> None:
        await self._json("GET", "/health")

    async def navigation(self) -> Navigation:
        standalone_payload, projects_payload = await asyncio.gather(
            self._json("GET", "/conversations"),
            self._json("GET", "/projects"),
        )
        standalone = tuple(
            Conversation.from_payload(item)
            for item in _object_list(standalone_payload, "conversations")
            if item.get("kind") in {None, "chat", "dynamic_dag"}
        )
        projects = tuple(
            Project.from_payload(item)
            for item in _object_list(projects_payload, "projects")
        )

        async def project_entry(project: Project) -> tuple[Project, tuple[Conversation, ...]]:
            payload = await self._json(
                "GET",
                f"/projects/{project.id}/conversations",
            )
            conversations = tuple(
                Conversation.from_payload(item)
                for item in _object_list(payload, "conversations")
                if item.get("kind") in {None, "chat", "dynamic_dag"}
            )
            return project, conversations

        entries = await asyncio.gather(*(project_entry(project) for project in projects))
        return Navigation(standalone=standalone, projects=tuple(entries))

    async def create_conversation(
        self,
        title: str,
        *,
        project_id: str | None = None,
        kind: Literal["chat", "dynamic_dag"] = "chat",
    ) -> Conversation:
        path = (
            "/conversations"
            if project_id is None
            else f"/projects/{project_id}/conversations"
        )
        payload = await self._json(
            "POST",
            path,
            json={"title": title, "kind": kind},
        )
        conversation = payload.get("conversation")
        if not isinstance(conversation, dict):
            raise DagentApiError("API response is missing the created conversation.")
        created = Conversation.from_payload(conversation)
        if kind == "dynamic_dag":
            await self._json(
                "POST",
                "/orchestration-sessions",
                json={
                    "conversation_id": created.id,
                    "project_id": created.project_id,
                    "kind": "dynamic_dag",
                    "ui_state": {"surface": "orchestration_workspace"},
                },
            )
        return created

    async def messages(self, conversation: Conversation) -> tuple[ConversationMessage, ...]:
        path = (
            f"/conversations/{conversation.id}/messages"
            if conversation.project_id is None
            else (
                f"/projects/{conversation.project_id}/conversations/"
                f"{conversation.id}/messages"
            )
        )
        payload = await self._json("GET", path)
        return tuple(
            ConversationMessage.from_payload(item)
            for item in _object_list(payload, "messages")
        )

    async def stream_message(
        self,
        conversation: Conversation,
        message: str,
        *,
        target: RunTarget,
        review_level: ReviewLevel,
    ) -> AsyncIterator[StreamEnvelope]:
        payload: dict[str, Any] = {
            "input": message,
            "target": target,
            "review_level": review_level,
            "conversation_id": conversation.id,
        }
        if conversation.project_id is not None:
            payload["project_id"] = conversation.project_id
        async for event in self._sse("/messages/stream", payload):
            yield event

    async def resume_review(
        self,
        conversation: Conversation,
        review: dict[str, Any],
        *,
        approved: bool,
        feedback: str,
        review_level: ReviewLevel,
        dag: dict[str, Any] | None,
    ) -> AsyncIterator[StreamEnvelope]:
        review_id = str(review.get("review_id") or "")
        if not review_id:
            raise DagentApiError("Review payload is missing review_id.")
        path = (
            f"/reviews/{review_id}/resume"
            if conversation.project_id is None
            else f"/projects/{conversation.project_id}/reviews/{review_id}/resume"
        )
        payload: dict[str, Any] = {
            "approved": approved,
            "review_level": review_level,
            "dag": dag if approved else None,
        }
        if feedback.strip():
            payload["feedback"] = feedback.strip()
        async for event in self._sse(path, payload):
            yield event

    async def cancel_run(self, run_id: str) -> bool:
        payload = await self._json("POST", f"/runs/{run_id}/cancel")
        return bool(payload.get("cancelled"))

    async def _sse(
        self,
        path: str,
        payload: dict[str, Any],
    ) -> AsyncIterator[StreamEnvelope]:
        try:
            async with self._client.stream("POST", path, json=payload) as response:
                if not response.is_success:
                    await response.aread()
                    raise DagentApiError(_response_error(response))
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if line == "":
                        event = _decode_sse_data(data_lines)
                        data_lines.clear()
                        if event is not None:
                            yield event
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip(" "))
                event = _decode_sse_data(data_lines)
                if event is not None:
                    yield event
        except httpx.HTTPError as exc:
            raise DagentApiError(f"Cannot reach dagent API at {self.base_url}: {exc}") from exc

    async def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise DagentApiError(f"Cannot reach dagent API at {self.base_url}: {exc}") from exc
        if not response.is_success:
            raise DagentApiError(_response_error(response))
        try:
            payload = response.json()
        except ValueError as exc:
            raise DagentApiError("dagent API returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise DagentApiError("dagent API returned an unexpected response.")
        return payload


def _decode_sse_data(lines: list[str]) -> StreamEnvelope | None:
    if not lines:
        return None
    raw = "\n".join(lines)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DagentApiError("dagent API returned an invalid stream event.") from exc
    if not isinstance(payload, dict):
        raise DagentApiError("dagent API returned an unexpected stream event.")
    event = StreamEnvelope.from_payload(payload)
    if not event.type:
        raise DagentApiError("dagent API stream event is missing its type.")
    return event


def _object_list(payload: dict[str, Any], field: str) -> list[dict[str, Any]]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise DagentApiError(f"API response is missing {field}.")
    return [item for item in value if isinstance(item, dict)]


def _response_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("detail"):
        detail = str(payload["detail"])
    else:
        detail = response.reason_phrase or "request failed"
    return f"dagent API request failed ({response.status_code}): {detail}"
