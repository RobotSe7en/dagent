"""Offline long-task context example: uv run python -m examples.context_management."""

from __future__ import annotations

import asyncio
from contextlib import closing
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

import httpx
from openai import AsyncOpenAI

import dagent
from examples.cache_usage import simulated_endpoint


@dagent.tool
def evidence(index: int) -> str:
    """Return one immutable synthetic observation, including the next step."""
    return f"Verified step {index}; next={index + 1}\n" + "sample evidence\n" * 700


class OfflineEndpoint:
    """Synthetic model responses; this does not measure a real prefix cache."""

    def __init__(self) -> None:
        self.step = 0
        self.summaries = 0
        self.fallback = simulated_endpoint()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            response = self.fallback(request)
            if request.url.path == "/openapi.json":
                spec = response.json()
                spec["paths"]["/v1/chat/completions"]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"].update(
                    {"tools": {}, "max_tokens": {}, "reasoning_effort": {}, "include_reasoning": {}}
                )
                return httpx.Response(200, json=spec)
            return response
        payload = json.loads(request.content)
        messages = payload["messages"]
        data = "\n".join(str(message.get("content", "")) for message in messages)
        summary = "Sources to summarize:" in data or messages[0]["content"].startswith("Produce a task checkpoint")
        calls = []
        if summary:
            self.summaries += 1
            # Use supplied aliases; real or inherited source IDs may reserve S0/S1.
            match = re.search(r"Sources to summarize: ([^\n]+)", data)
            ids = ([token.strip() for token in match.group(1).split(",")] if match
                   else re.findall(r"\[Source (S\d+);", data))
            if not ids:
                ids = re.findall(r"\[(S\d+)(?:, S\d+)*\]", data)
            content = (
                "### Completed Work\n"
                "- Earlier observations were received; use their source index for exact details. "
                f"[{ids[-1]}]\n\n"
                "### Next Step\n"
                f"- Continue from the latest retained tool result. [{ids[-1]}]"
            )
        else:
            self.step += 1
            content = "Finished all synthetic observations." if self.step > 24 else ""
            if not content:
                calls = [{"id": f"call_{self.step}", "type": "function", "function": {
                    "name": "tool_evidence", "arguments": json.dumps({"index": self.step}),
                }}]
        message = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = calls
        finish = "tool_calls" if calls else "stop"
        body = {"id": "offline_context", "object": "chat.completion", "created": 0,
                "model": "offline", "choices": [{"index": 0, "finish_reason": finish, "message": message}]}
        if not payload.get("stream"):
            return httpx.Response(200, json=body)
        chunk = {"id": body["id"], "object": "chat.completion.chunk", "created": 0, "model": "offline",
                 "choices": [{"index": 0, "finish_reason": finish, "delta": message}]}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")


async def main() -> None:
    endpoint = OfflineEndpoint()
    with TemporaryDirectory(prefix="dagent-context-") as temporary:
        async with AsyncOpenAI(
            api_key="offline", base_url="https://model.example/v1", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(endpoint)),
        ) as client:
            provider = dagent.Provider(
                base_url="https://model.example/v1", model="offline", client=client,
                protocol="chat_completions", token_counting="heuristic",
                context_window_tokens=8192, max_output_tokens=512,
            )
            with closing(dagent.Runner(provider=provider, workspace=temporary, skill_roots=[],
                                      context_observability=True)) as runner:
                agent = dagent.ToolAgent(
                    # A real text reader is required for recoverable shortening;
                    # unreadable bodies (including NUL text) must fit intact.
                    profile="conversation", capabilities=[evidence, "tool.read_file"], max_steps=32,
                    context=dagent.ContextPolicy(
                        # This 8K offline demo returns short synthetic checkpoints.
                        # Real workloads can leave summary_max_tokens at its 8192 default.
                        compaction_strategy="cache_aware", summary_max_tokens=512,
                        max_tool_result_tokens=1024,
                        # Permit reference continuation under hard pressure, including
                        # when preflight leaves too little output room for a summary.
                        allow_reference_fallback=True,
                    ),
                )
                events = [event async for event in runner.stream(
                    agent, input="Collect 24 synthetic observations.",
                    workspace_path=Path(temporary) / "first",
                )]
                result = events[-1].data.result
                assert result is not None
                calls = [event.data for event in events if event.type == "model.call.observed"]
                assert {call.purpose for call in calls} == {"generation", "compaction"}
                assert any(event.type == "context.compaction.ended" for event in events)
                assert result.status == "completed"
                assert endpoint.summaries >= 2
                assert all(usage.estimated_input_tokens <= usage.input_budget_tokens for usage in result.context_usage)
                reference = result.conversation.summary.compaction_record
                assert reference is not None
                continued = await runner.run(agent, input="Continue using the preserved evidence.",
                                             conversation=result.conversation,
                                             workspace_path=Path(temporary) / "continued")
                assert continued.status == "completed"
                assert (Path(continued.workspace_path) / reference.path).is_file()
                print(result.output_text)
                print(f"Observed HTTP requests: {len(calls)} (usage may be unknown).")
                print(f"Summary requests: {endpoint.summaries}; source index restored across runs.")
                print("Offline simulation; run the vLLM soak example to measure real cache usage.")


if __name__ == "__main__":
    asyncio.run(main())
