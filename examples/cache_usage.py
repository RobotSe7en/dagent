"""Inspect synthetic cache counts offline: uv run python -m examples.cache_usage."""

from __future__ import annotations

import asyncio
from contextlib import closing
from itertools import cycle
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from openai import AsyncOpenAI

import dagent


def simulated_endpoint():
    counts = cycle((80, 0, None))

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json={"paths": {
                path: {"post": {"requestBody": {"content": {"application/json": {
                    "schema": {"properties": {field: {} for field in fields}},
                }}}}}
                for path, fields in (
                    ("/v1/chat/completions", ("messages", "stream")),
                    ("/v1/responses", ("input", "stream")),
                )
            }})
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "simulated"})
        payload = json.loads(request.content)
        assert "capture_cache_usage" not in payload
        cached = next(counts)
        if request.url.path.endswith("/responses"):
            body = {
                "id": "resp_example", "object": "response", "created_at": 0,
                "model": "test", "status": "completed", "output": [{
                    "type": "message", "id": "msg_example", "role": "assistant",
                    "status": "completed", "content": [{
                        "type": "output_text", "text": "done", "annotations": [],
                    }],
                }],
                "usage": {"input_tokens": 1000, "output_tokens": 20, "total_tokens": 1020,
                          "input_tokens_details": {"cached_tokens": cached},
                          "output_tokens_details": {"reasoning_tokens": 0}},
            }
            events = [{"type": "response.completed", "response": body, "sequence_number": 1}]
        else:
            body = {
                "id": "chat_example", "object": "chat.completion", "created": 0,
                "model": "test", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "done"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020,
                          "prompt_tokens_details": {"cached_tokens": cached}},
            }
            chunk = {"id": "chat_example", "object": "chat.completion.chunk", "created": 0,
                     "model": "test"}
            events = [
                {**chunk, "choices": [{"index": 0, "finish_reason": "stop", "delta": {"content": "done"}}]},
                {**chunk, "choices": [], "usage": body["usage"]},
            ]
        if not payload.get("stream"):
            return httpx.Response(200, json=body)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="".join(f"data: {json.dumps(event)}\n\n" for event in events)
                              + "data: [DONE]\n\n")

    return handle


async def main() -> None:
    print("Simulated endpoint: synthetic counts, not a cache benchmark.")
    with TemporaryDirectory(prefix="dagent-cache-usage-") as temporary:
        async with AsyncOpenAI(
            base_url="https://model.example/v1", api_key="offline",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(simulated_endpoint())),
        ) as client:
            for protocol in ("chat_completions", "responses"):
                provider = dagent.Provider(
                    base_url="https://model.example/v1", model="test", client=client,
                    protocol=protocol, token_counting="heuristic",
                    capture_cache_usage=True, stream_include_usage=True,
                )
                with closing(dagent.Runner(
                    provider=provider, workspace=Path(temporary) / protocol, skill_roots=[],
                )) as runner:
                    agent = dagent.ToolAgent(profile="conversation", capabilities=[])
                    for stream in (False, True):
                        for expected in (80, 0, None):
                            if stream:
                                events = [event async for event in runner.stream(agent, input="Hello")]
                                result = events[-1].data.result
                            else:
                                result = await runner.run(agent, input="Hello")
                            assert result.status == "completed"
                            usage = result.conversation.items[-1].usage
                            assert usage.cached_input_tokens == expected
                            ratio = (usage.cached_input_tokens / usage.input_tokens
                                     if usage.cached_input_tokens is not None and usage.input_tokens
                                     else None)
                            display = "unknown" if ratio is None else f"{ratio:.0%}"
                            print(f"{protocol} stream={stream}: cached={usage.cached_input_tokens}, ratio={display}")


if __name__ == "__main__":
    asyncio.run(main())
