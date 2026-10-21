"""Offline render rejection: uv run python -m examples.prepared_count_reprepare.

The host chooses the rejection classifier, fallback, and cache lifetime. dagent
only prepares, counts, and checks the replacement before sending it.
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import replace
import json
from tempfile import TemporaryDirectory

import httpx
from openai import AsyncOpenAI, BadRequestError

import dagent
from dagent.providers import ProviderReprepareRequired, ProviderTokenCountError


class RenderFallbackProvider:
    """Example host policy for one model/endpoint; this is not SDK policy."""

    def __init__(self, provider):
        self.provider = provider
        self.high_rejected = False
        self.counted = []

    def __getattr__(self, name):
        return getattr(self.provider, name)

    async def prepare_call(self, request, *, stream=False):
        effort = request.reasoning_effort or self.provider.config.reasoning_effort
        if self.high_rejected and effort == "high" and not request.omit_reasoning_effort:
            request = replace(request, reasoning_effort="medium")
        return await self.provider.prepare_call(request, stream=stream)

    async def count_prepared(self, call):
        try:
            count = await self.provider.count_prepared(call)
        except ProviderTokenCountError as error:
            cause = error.__cause__
            # This classifier recognizes only the synthetic endpoint's explicit
            # rejection. A real host must match its own server's error contract.
            if not isinstance(cause, BadRequestError):
                raise
            rejected = cause.response.json().get("error", {})
            if (self.high_rejected or call.payload.get("reasoning_effort") != "high"
                    or rejected.get("param") != "reasoning_effort"
                    or rejected.get("code") != "unsupported_value"):
                raise
            self.high_rejected = True
            raise ProviderReprepareRequired(cause, metadata=call.metadata) from error
        self.counted.append(call)
        return count


async def main() -> None:
    rendered, sent = [], []

    def endpoint(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json={
                "info": {"title": "vLLM OpenAI-Compatible API"},
                "paths": {
                    "/v1/chat/completions": {"post": {"requestBody": {"content": {
                        "application/json": {"schema": {"properties": {
                            "messages": {}, "reasoning_effort": {}, "max_completion_tokens": {},
                            "include_reasoning": {}, "tools": {},
                        }}},
                    }}}},
                    "/v1/chat/completions/render": {"post": {}},
                    "/tokenize": {"post": {}},
                },
            })
        if request.url.path == "/version":
            return httpx.Response(200, json={"version": "offline-example"})
        body = json.loads(request.content)
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"count": 3, "max_model_len": 4096})
        if request.url.path.endswith("/render"):
            rendered.append(body)
            if body.get("reasoning_effort") == "high":
                return httpx.Response(400, json={"error": {
                    "param": "reasoning_effort", "code": "unsupported_value",
                    "message": "This example model rejects high effort.",
                }})
            return httpx.Response(200, json={"token_ids": [10, 20, 30], "max_model_len": 4096})
        sent.append(body)
        return httpx.Response(200, json={
            "id": "chat-example", "object": "chat.completion", "created": 0,
            "model": "offline", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "done"}}],
        })

    with TemporaryDirectory(prefix="dagent-reprepare-") as workspace:
        async with AsyncOpenAI(
            base_url="http://model.example/v1", api_key="offline", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(endpoint)),
        ) as client:
            native = dagent.Provider(
                base_url="http://model.example/v1", model="offline", client=client,
                protocol="chat_completions", reasoning_effort="high",
                context_window_tokens=4096, max_output_tokens=512, token_counting="vllm",
            )
            wrapper = RenderFallbackProvider(native)
            with closing(dagent.Runner(provider=wrapper, workspace=workspace, skill_roots=[])) as runner:
                result = await runner.run(dagent.ToolAgent(
                    profile="conversation", capabilities=[],
                    context=dagent.ContextPolicy(compaction_strategy="cache_aware"),
                ), input="Hello")
            assert result.status == "completed"
            assert [body["reasoning_effort"] for body in rendered] == ["high", "medium"]
            assert sent == [rendered[-1]] == [wrapper.counted[-1].payload]
            assert native.config.reasoning_effort == "high"
            print("render rejected high; wrapper selected medium; SDK counted and sent the same replacement")


if __name__ == "__main__":
    asyncio.run(main())
