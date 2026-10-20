"""Inspect reasoning mappings with a simulated endpoint, entirely offline.

Run: uv run python -m examples.reasoning_mapping
"""

from __future__ import annotations

import asyncio
import json

import httpx
from openai import AsyncOpenAI

import dagent
from dagent.config import ReasoningMapping
from dagent.providers.model_io import ModelRequest, ModelUserInput


def endpoint(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/openapi.json":
        # This endpoint does not advertise built-in effort controls.
        return httpx.Response(200, json={"paths": {
            path: {"post": {"requestBody": {"content": {"application/json": {
                "schema": {"properties": {"messages": {}, "input": {}}},
            }}}}}
            for path in ("/v1/chat/completions", "/v1/responses")
        }})
    if request.url.path == "/version":
        return httpx.Response(200, json={"version": "offline-example"})
    payload = json.loads(request.content)
    assert "include_reasoning" not in payload
    if request.url.path.endswith("/responses"):
        return httpx.Response(200, json={
            "id": "response-example", "object": "response", "created_at": 0,
            "status": "completed", "output": [{
                "type": "message", "id": "message-example", "role": "assistant",
                "content": [{"type": "output_text", "text": "done", "annotations": []}],
            }],
        })
    assert payload["custom_reasoning"]["level"] in {"deep", "low"}
    assert "reasoning_effort" not in payload
    return httpx.Response(200, json={
        "id": "chat-example", "object": "chat.completion", "created": 0,
        "model": "private-model", "choices": [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "done"},
        }],
    })


async def main() -> None:
    async with AsyncOpenAI(
        base_url="http://model.example/v1", api_key="offline",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(endpoint)),
    ) as client:
        for protocol in ("chat_completions", "responses"):
            provider = dagent.Provider(
                base_url="http://model.example/v1", model="private-model", client=client,
                protocol=protocol, reasoning_effort="high",
                reasoning_mapping={
                    "chat_completions": ReasoningMapping(
                        field="custom_reasoning.level", values={"high": "deep"},
                    ),
                    "responses": ReasoningMapping(
                        field="reasoning.effort", values={"high": "xhigh"},
                    ),
                },
            )
            for purpose, effort in (("generation", None), ("compaction", "low")):
                response = await provider.complete(ModelRequest(
                    instructions="Be helpful.",
                    items=(ModelUserInput(source_id="example", content="Hello"),),
                    reasoning_effort=effort, purpose=purpose,
                ))
                metadata = response.metadata
                assert metadata is not None
                assert metadata.effective_reasoning_effort == (
                    "low" if purpose == "compaction" else
                    "deep" if protocol == "chat_completions" else "xhigh"
                )
                print(f"{protocol} {purpose}: {metadata.requested_reasoning_effort} -> {metadata.effective_reasoning_effort}")


if __name__ == "__main__":
    asyncio.run(main())
