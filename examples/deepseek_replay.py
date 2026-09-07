"""Opt-in, three-request DeepSeek replay acceptance; never print request bodies."""

import asyncio
import json
import os
from tempfile import TemporaryDirectory

import httpx
from openai import AsyncOpenAI

import dagent
from dagent.config import ProviderConfig
from dagent.providers import OpenAICompatibleProvider


class BoundedTransport(httpx.AsyncBaseTransport):
    """Hold synthetic request snapshots in memory only, with a hard POST cap."""

    def __init__(self):
        self.transport = httpx.AsyncHTTPTransport(retries=0)
        self.bodies: list[dict] = []

    async def handle_async_request(self, request):
        if request.method == "POST":
            if request.url.path != "/chat/completions" or len(self.bodies) >= 3:
                raise RuntimeError("Acceptance request limit reached.")
            if len(request.content) > 16000:
                raise RuntimeError("Acceptance input size limit reached.")
            body = json.loads(request.content)
            if body.get("max_tokens") != 1024:
                raise RuntimeError("Acceptance output limit must be 1024 tokens.")
            self.bodies.append(body)
        return await self.transport.handle_async_request(request)

    async def aclose(self):
        await self.transport.aclose()


@dagent.tool
def lookup_test_value() -> str:
    """Read the synthetic acceptance value. Call this once to obtain the answer."""
    return "acceptance-value-42"


async def main():
    if os.environ.get("DAGENT_RUN_DEEPSEEK_TESTS") != "1":
        raise SystemExit("Opt in with DAGENT_RUN_DEEPSEEK_TESTS=1; this spends API credit.")
    key = os.environ.get("API_KEY")
    if not key:
        raise SystemExit("Provide the test credential through API_KEY.")
    transport = BoundedTransport()
    async with AsyncOpenAI(
        api_key=key, base_url="https://api.deepseek.com", max_retries=0, timeout=30,
        http_client=httpx.AsyncClient(transport=transport, timeout=30),
    ) as client:
        config = ProviderConfig(
            api_key=key, base_url="https://api.deepseek.com", model="deepseek-v4-flash",
            protocol="chat_completions", token_counting="heuristic",
            reasoning_effort="high", max_output_tokens=1024,
            extra_body={"thinking": {"type": "enabled"}},
        )
        provider = OpenAICompatibleProvider(config, client=client)
        with TemporaryDirectory(prefix="dagent-deepseek-acceptance-") as workspace:
            runner = dagent.Runner(provider=provider, workspace=workspace,
                                   capabilities=[lookup_test_value], skill_roots=[])
            try:
                result = await runner.run(
                    dagent.ToolAgent(profile="conversation", capabilities=["tool.lookup_test_value"], max_steps=2,
                                     context=dagent.ContextPolicy(reasoning_replay="active_run")),
                    input="Call lookup_test_value exactly once, then report its returned value and stop.",
                )
                assert len(transport.bodies) == 2, "Expected exactly one tool exchange."
                original = next(m for m in result.conversation.items
                                if isinstance(m, dagent.AssistantMessage) and m.tool_calls)
                second = transport.bodies[1]
                replayed = [m["reasoning_content"] for m in second["messages"]
                            if m.get("reasoning_content")]
                assert original.reasoning and replayed == [original.reasoning]
                assert "acceptance-value-42" in result.output_text
                explicit = OpenAICompatibleProvider(
                    config.model_copy(update={"chat_reasoning_field": "reasoning_content"}),
                    client=client,
                )
                comparison = await explicit.chat(second["messages"], tools=second["tools"])
                assert "acceptance-value-42" in comparison.content
                assert transport.bodies[2]["messages"] == second["messages"]
                latest = next(m for m in reversed(result.conversation.items)
                              if isinstance(m, dagent.AssistantMessage))
                print(json.dumps({"accepted": True, "model": config.model,
                    "request_reasoning": latest.model_call.request_reasoning.model_dump(mode="json"),
                    "generation_requests": len(transport.bodies),
                    "replayed_items": len(replayed),
                    "replayed_characters": sum(map(len, replayed)),
                    "auto_matches_explicit_request": True}))
            finally:
                runner.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        # Exception text can contain provider data. Only emit the error type.
        raise SystemExit(f"Acceptance failed ({type(exc).__name__}); no automatic retry.") from None
