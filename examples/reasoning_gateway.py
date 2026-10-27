"""Run a reasoning/tool exchange through an open-schema gateway, offline.

Run: uv run python -m examples.reasoning_gateway
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from openai import AsyncOpenAI

import dagent


class GatewayEndpoint:
    """A Chat-only gateway: effort is declared; assistant extras are allowed."""

    def __init__(self):
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json={
                "info": {"title": "Scheduler", "version": "offline"},
                "paths": {"/v1/chat/completions": {"post": {"requestBody": {"content": {
                    "application/json": {"schema": {"type": "object", "properties": {
                        "messages": {"type": "array", "items": {
                            "type": "object", "additionalProperties": True,
                            "properties": {"role": {"type": "string"}, "content": {}},
                        }},
                        "reasoning_effort": {"type": "string"}, "tools": {"type": "array"},
                        "stream": {"type": "boolean"}, "max_tokens": {"type": "integer"},
                    }}},
                }}}}},
            })
        if request.method == "GET":
            return httpx.Response(404)
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["reasoning_effort"] == "medium"
        assert "include_reasoning" not in body
        self.bodies.append(body)
        assert len(self.bodies) <= 2
        first = len(self.bodies) == 1
        message = {"role": "assistant", "content": "" if first else "gateway-value-42"}
        if first:
            message.update(reasoning_content="I should look up the synthetic value.", tool_calls=[{
                "id": "lookup-1", "type": "function", "function": {
                    "name": body["tools"][0]["function"]["name"], "arguments": "{}",
                },
            }])
        else:
            assistant = next(item for item in body["messages"] if item["role"] == "assistant")
            assert assistant["reasoning_content"] == "I should look up the synthetic value."
            assert "reasoning" not in assistant
            assert any(item["role"] == "tool" for item in body["messages"])
        finish = "tool_calls" if first else "stop"
        result = {"id": "gateway-1", "object": "chat.completion", "created": 0,
                  "model": "private-model", "choices": [
                      {"index": 0, "message": message, "finish_reason": finish},
                  ]}
        if not body.get("stream"):
            return httpx.Response(200, json=result)
        if first:
            message["tool_calls"][0]["index"] = 0
        chunk = {**result, "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": message, "finish_reason": finish},
        ]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                             text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n")


async def exercise(workspace: Path, *, stream: bool = False) -> dict:
    endpoint = GatewayEndpoint()
    tool_calls = []

    @dagent.tool
    def lookup() -> str:
        """Return the synthetic value."""
        tool_calls.append("lookup")
        return "gateway-value-42"

    async with AsyncOpenAI(
        base_url="http://gateway.example/v1", api_key="offline", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(endpoint)),
    ) as client:
        provider = dagent.Provider(
            base_url="http://gateway.example/v1", model="private-model", client=client,
            reasoning_effort="medium", chat_reasoning_field="reasoning_content",
            token_counting="heuristic", max_output_tokens=512,
        )
        capabilities = await provider.inspect_capabilities()
        assert capabilities.chat_completions.replay_reasoning_content == "unknown"
        runner = dagent.Runner(provider=provider, workspace=workspace,
                               capabilities=[lookup], skill_roots=[])
        agent = dagent.ToolAgent(
            profile="conversation", capabilities=["tool.lookup"], max_steps=2,
            context=dagent.ContextPolicy(reasoning_replay="active_run"),
        )
        try:
            if stream:
                events = [event async for event in runner.stream(agent, input="Look up the value.")]
                assert events[-1].type == "run.finished"
                result = events[-1].data.result
            else:
                result = await runner.run(agent, input="Look up the value.")
            assert result.output_text == "gateway-value-42"
            assert tool_calls == ["lookup"]
            assert len(endpoint.bodies) == 2
            return {"stream": stream, "model_requests": len(endpoint.bodies),
                    "tool_executions": len(tool_calls), "replay_capability": "unknown",
                    "output": result.output_text}
        finally:
            runner.close()


async def main() -> None:
    with TemporaryDirectory(prefix="dagent-gateway-") as workspace:
        for stream in (False, True):
            print(json.dumps(await exercise(Path(workspace) / str(stream), stream=stream)))


if __name__ == "__main__":
    asyncio.run(main())
