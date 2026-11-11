"""Released 030e6b8 wire/state snapshots, captured before retry/retention changes.

UUID producers, UTC/elapsed clocks and prompt workspace labels are fixed before
counting/hashing. Only temporary file paths are normalized after serialization;
every other serialized field is compared.
Regenerate deliberately with DAGENT_CAPTURE_BASELINE=1 on the baseline revision.
"""

from contextlib import closing
from datetime import datetime, timezone
from itertools import count
import json
import gzip
import hashlib
import os
from pathlib import Path
import sys
from uuid import UUID

import pytest

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall
from tests.test_conversation_changes import Host, invoke
from tests.test_cache_usage import _completion, _usage
from tests.test_reasoning_mapping import _endpoint
from tests.test_compaction_strategy import checkpoint, request_aliases


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@pytest.fixture
def stable_clocks(monkeypatch):
    ids = count(1)
    for name, module in tuple(sys.modules.items()):
        if name.startswith("dagent."):
            if hasattr(module, "uuid4"):
                monkeypatch.setattr(module, "uuid4", lambda: UUID(int=next(ids)))
            if hasattr(module, "datetime"):
                monkeypatch.setattr(module, "datetime", FixedDateTime)
            if hasattr(module, "monotonic"):
                monkeypatch.setattr(module, "monotonic", lambda: 10.0)

    # Fix the prompt's workspace label before the provider hashes its payload.
    # Files still go to tmp_path; changing serialized fingerprints after the
    # fact would hide incompatibilities.
    from dagent.state import PromptBuilder
    original = PromptBuilder.build_system_message

    def stable_prompt(self, request):
        message = original(self, request)
        if request.workspace_path is not None:
            message["content"] = message["content"].replace(str(request.workspace_path), "/baseline/work")
        return message

    monkeypatch.setattr(PromptBuilder, "build_system_message", stable_prompt)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["ordinary", "review", "failed_continue", "compaction", "child"])
async def test_released_response_contract(tmp_path, stable_clocks, scenario):
    @dagent.tool(risk="medium" if scenario == "review" else "low")
    def echo() -> str:
        return "evidence"

    initial = None
    policy = dagent.ContextPolicy()
    responses = [ChatResponse(tool_calls=[ToolCall(id="call", name="tool_echo", arguments={})]),
                 ChatResponse(content="done")]
    if scenario == "failed_continue":
        responses = [ChatResponse(), ChatResponse(content="continued")]
    if scenario == "compaction":
        initial = dagent.ConversationState(items=tuple(
            item for index in range(4) for item in (
                dagent.UserMessage(run_id=f"old_{index}", content="question " * 200),
                dagent.AssistantMessage(run_id=f"old_{index}", content="evidence " * 300),
            )
        ))
        responses.insert(0, ChatResponse(content="bounded summary"))
        policy = dagent.ContextPolicy(compaction_trigger_ratio=.4, summary_max_tokens=128)
    if scenario == "child":
        responses = [ChatResponse(tool_calls=[ToolCall(id="delegate", name="agent_helper", arguments={"prompt": "help"})]),
                     ChatResponse(content="child answer"), ChatResponse(content="parent answer")]
    provider = MockProvider(responses)
    if scenario == "compaction":
        provider.configured_context_window_tokens = 4096
        provider.max_output_tokens = 512
    snapshots = []
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        agent = dagent.ToolAgent(profile="conversation", capabilities=[echo], context=policy,
                                 review="careful" if scenario == "review" else "fast")
        if scenario == "child":
            runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[]))
            agent = dagent.ToolAgent(profile="conversation", capabilities=[], agents=["agent.helper"])
        host = Host(initial, runner)

        def capture(result, events):
            snapshots.append({
                "result": result.model_dump(mode="json"),
                "state": runner.run_state(result.run_id).model_dump(mode="json"),
                "checkpoint": runner.run_checkpoint(result.run_id).model_dump(mode="json"),
                "plan": result.plan.model_dump(mode="json"),
                "events": [event.model_dump(mode="json") for event in events],
                "batches": [batch.model_dump(mode="json") for batch in host.batches],
                "requests": list(provider.requests),
            })
            assert host.state == result.conversation

        result, events = await invoke(runner, agent, host, True, input="echo", conversation=initial,
                                      run_id="baseline", workspace_path=tmp_path / "work")
        capture(result, events)
        if scenario == "review":
            events = [event async for event in runner.resume_stream(result.review.approve(), checkpoint=result.checkpoint,
                      on_conversation_change=host.save)]
            capture(events[-1].data.result, events)
        if scenario == "failed_continue":
            result, events = await invoke(runner, agent, host, True, input="continue", conversation=result.conversation,
                                          run_id="continued", workspace_path=tmp_path / "work")
            capture(result, events)
    payload = json.dumps(snapshots, sort_keys=True, ensure_ascii=False, indent=2).replace(str(tmp_path), "<workspace>") + "\n"
    fixture = Path(__file__).parent / "fixtures" / f"response_baseline_{scenario}.json"
    if os.environ.get("DAGENT_CAPTURE_BASELINE") == "1":
        fixture.write_text(payload)
    assert payload == fixture.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["legacy", "chunked", "cache_aware"])
async def test_released_long_context_contract(tmp_path, stable_clocks, strategy):
    turns = summaries = 0

    @dagent.tool
    def query(index: int) -> str:
        return f"page {index}: " + "evidence " * 100

    def respond(request):
        nonlocal turns, summaries
        body = json.loads(request.content)
        text = str(body["messages"])
        if "Sources to summarize:" in text or body["messages"][0]["content"].startswith("Produce a task checkpoint"):
            summaries += 1
            aliases = request_aliases(text)
            content = checkpoint(requirements=[("Collect pages.", [aliases[0]])],
                                 next_step=[("Continue.", [aliases[-1]])])
        elif body["messages"][0]["content"].startswith("Summarize earlier conversation data"):
            summaries += 1
            content = "Collected earlier pages; continue."
        else:
            turns += 1
            return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"),
                content="done" if turns > 32 else "",
                calls=() if turns > 32 else (ToolCall(id=f"call_{turns}", name="tool_query", arguments={"index": turns}),))
        return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"), content=content)

    async with _endpoint(protocol="chat_completions", token_counting="heuristic", context_window_tokens=4096,
                         max_output_tokens=512, responder=respond) as (provider, posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            host = Host(runner=runner)
            result, events = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[query],
                context=dagent.ContextPolicy(compaction_strategy=strategy, summary_max_tokens=512)), host, True,
                input="Collect pages.", run_id="long", workspace_path=tmp_path / "work")
            assert result.status == "completed" and summaries > 0
            assert host.state == result.conversation
            value = {"result": result.model_dump(mode="json"), "checkpoint": result.checkpoint.model_dump(mode="json"),
                     "state": runner.run_state(result.run_id).model_dump(mode="json"),
                     "events": [event.model_dump(mode="json") for event in events],
                     "batches": [batch.model_dump(mode="json") for batch in host.batches],
                     "requests": [json.loads(request.content) for request in posts]}
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False).replace(str(tmp_path), "<workspace>")
    fixture = Path(__file__).parent / "fixtures" / f"response_baseline_long_{strategy}.json.gz"
    if os.environ.get("DAGENT_CAPTURE_BASELINE") == "1":
        fixture.write_bytes(gzip.compress(payload.encode(), mtime=0))
    expected = gzip.decompress(fixture.read_bytes()).decode()
    if payload != expected:
        (tmp_path / "actual.json").write_text(payload)
        (tmp_path / "expected.json").write_text(expected)
    assert hashlib.sha256(payload.encode()).hexdigest() == hashlib.sha256(expected.encode()).hexdigest()
