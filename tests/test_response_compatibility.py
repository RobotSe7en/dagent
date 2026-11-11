"""Released 030e6b8 wire/state snapshots, captured before retry/retention changes.

UUID producers, UTC clocks and elapsed clocks are fixed, not removed. Only the
temporary workspace prefix is replaced; every other serialized field is compared.
Regenerate deliberately with DAGENT_CAPTURE_BASELINE=1 on the baseline revision.
"""

from contextlib import closing
from datetime import datetime, timezone
from itertools import count
import json
import os
from pathlib import Path
import sys
from uuid import UUID

import pytest

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall
from tests.test_conversation_changes import Host, invoke


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["ordinary", "review", "failed_continue", "compaction", "child"])
async def test_released_response_contract(tmp_path, monkeypatch, scenario):
    ids = count(1)
    for name, module in tuple(sys.modules.items()):
        if name.startswith("dagent."):
            if hasattr(module, "uuid4"):
                monkeypatch.setattr(module, "uuid4", lambda: UUID(int=next(ids)))
            if hasattr(module, "datetime"):
                monkeypatch.setattr(module, "datetime", FixedDateTime)
            if hasattr(module, "monotonic"):
                monkeypatch.setattr(module, "monotonic", lambda: 10.0)

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
