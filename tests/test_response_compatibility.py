"""Released public result, event, continuation and wire contracts.

Structural tests check public shapes. Snapshot tests compare SHA-256 digests of
complete normal-mode wire/state payloads captured on the baseline revision named
in fixtures/response_normal_contract_hashes.json; full snapshots are ignored
local debugging artifacts. UUID producers, UTC/elapsed clocks and prompt
workspace labels are fixed before counting/hashing. Only temporary file paths are
normalized after serialization; every other serialized field is compared.
Mismatches save actual.json in tmp_path, plus expected.json when a local snapshot
is available. Regenerate snapshots and digests deliberately with
DAGENT_CAPTURE_BASELINE=1 on the baseline revision.
"""

from contextlib import closing
from datetime import datetime, timezone
import gzip
import hashlib
from itertools import count
import json
import os
from pathlib import Path
import sys
from uuid import UUID

import pytest

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall
from tests.test_cache_usage import _completion, _usage
from tests.test_compaction_strategy import checkpoint, request_aliases
from tests.test_conversation_changes import Host
from tests.test_reasoning_mapping import _endpoint


async def invoke(runner, agent, stream, **kwargs):
    if stream:
        events = [event async for event in runner.stream(agent, **kwargs)]
        return events[-1].data.result, events
    return await runner.run(agent, **kwargs), []


def assert_public_result(runner, result, events):
    assert result.kind == "tool"
    assert result.trace is not None
    assert result.conversation.schema_version == 4
    assert result.state.schema_version == 6
    assert runner.run_state(result.run_id) == result.state

    payload = result.model_dump(mode="json")
    assert set(payload) == {"state", "output_text", "new_items", "usage", "output_value"}
    assert dagent.RunResult.model_validate(payload).model_dump(mode="json") == payload
    saved = dagent.RunCheckpoint.model_validate_json(result.checkpoint.model_dump_json())
    assert saved.schema_version == 9
    assert saved.state == result.state
    assert saved.plan == result.plan
    assert saved.usage == result.usage
    assert runner.run_checkpoint(result.run_id) == saved
    assert saved.plan.response_retries == 0
    assert "response_retries" not in saved.plan.model_dump(mode="json")

    if events:
        assert events[0].type == "run.started"
        assert events[-1].type == "run.finished"
        assert events[-1].data.result.model_dump(mode="json") == payload
        assert "conversation.changed" not in {event.type for event in events}
        for event in events:
            wire = event.model_dump(mode="json")
            assert dagent.RunStreamEvent.model_validate(wire).model_dump(mode="json") == wire


def test_released_record_defaults_are_explicit():
    conversation = dagent.ConversationState.model_validate({"id": "saved"})
    assert conversation.model_dump(mode="json") == {
        "schema_version": 4, "id": "saved", "revision": 0, "summary": None, "items": [],
    }
    state = dagent.RunState.model_validate({"run_id": "saved", "kind": "tool", "status": "completed"})
    fields = ("schema_version", "review_level", "runtime_mode", "execution", "dynamic_adjust",
              "planner_frontend", "capability_scope", "input_artifact_files")
    payload = state.model_dump(mode="json")
    assert {key: payload[key] for key in fields} == {
        "schema_version": 6, "review_level": "fast", "runtime_mode": "auto", "execution": "local",
        "dynamic_adjust": True, "planner_frontend": "typed_spec",
        "capability_scope": {"capability_ids": None, "skills": None}, "input_artifact_files": [],
    }
    assert "_terminal_error" not in payload and "_checkpoint_eligible" not in payload
    assert dagent.ContextPolicy().model_dump(mode="json") == {
        "reasoning_replay": "active_run", "compaction_trigger_ratio": .8, "compaction_retain_ratio": .16,
        "summary_max_tokens": 8192, "compaction_reasoning_effort": "low", "max_tool_result_tokens": 2048,
        "max_total_tool_result_tokens": 16384, "token_safety_margin": .15,
    }


@pytest.mark.parametrize("event_type", ["response.started", "response.finished"])
def test_released_response_event_defaults_are_explicit(event_type):
    event = dagent.RunStreamEvent.model_validate({"type": event_type, "data": {"response_id": "response"}})
    assert event.model_dump(mode="json") == {
        "type": event_type, "sequence": 0, "run_id": None,
        "data": {"response_id": "response", "model_step": None, "run_id": None,
                 "dag_id": None, "node_id": None, "parent_capability_id": None},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("scenario", ["ordinary", "review", "failed_continue", "compaction", "child"])
async def test_released_response_contract(tmp_path, scenario, stream):
    executed = []

    @dagent.tool(risk="medium" if scenario == "review" else "low")
    def echo() -> str:
        executed.append("echo")
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
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        agent = dagent.ToolAgent(profile="conversation", capabilities=[echo], context=policy,
                                 review="careful" if scenario == "review" else "fast")
        if scenario == "child":
            runner.add_agent(dagent.ToolAgent(profile="conversation", name="helper", capabilities=[]))
            agent = dagent.ToolAgent(profile="conversation", capabilities=[], agents=["agent.helper"])

        result, events = await invoke(runner, agent, stream, input="echo", conversation=initial,
                                      run_id="contract", workspace_path=tmp_path / "work")
        assert_public_result(runner, result, events)
        if scenario == "review":
            assert result.status == "awaiting_review"
            assert result.review is not None
            assert executed == []
            saved = dagent.RunCheckpoint.model_validate_json(result.checkpoint.model_dump_json())
            if stream:
                events = [event async for event in runner.resume_stream(result.review.approve(), checkpoint=saved)]
                result = events[-1].data.result
            else:
                result, events = await runner.resume(result.review.approve(), checkpoint=saved), []
            assert_public_result(runner, result, events)
        if scenario == "failed_continue":
            assert result.status == "failed"
            assert result.error.code == "empty_response"
            saved = dagent.ConversationState.model_validate_json(result.conversation.model_dump_json())
            result, events = await invoke(runner, agent, stream, input="continue", conversation=saved,
                                          run_id="continued", workspace_path=tmp_path / "work")
            assert_public_result(runner, result, events)
            assert [item.content for item in result.conversation.items if isinstance(item, dagent.UserMessage)] == ["echo", "continue"]

        assert result.status == "completed"
        assert result.output_text == {"child": "parent answer", "failed_continue": "continued"}.get(scenario, "done")
        assert executed == ([] if scenario in {"child", "failed_continue"} else ["echo"])
        if scenario == "compaction":
            assert result.conversation.summary is not None
        if scenario not in {"child", "failed_continue"}:
            tool_results = [item for item in result.conversation.items if isinstance(item, dagent.ToolResultMessage)]
            assert len(tool_results) == 1
            assert tool_results[0].call_id == "call"
            assert tool_results[0].status == "completed"
            assert tool_results[0].content.text == "evidence"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("strategy", ["legacy", "chunked", "cache_aware"])
async def test_released_long_context_contract(tmp_path, strategy, stream):
    turns = summaries = 0
    executed = []
    request_streams = []

    @dagent.tool
    def query(index: int) -> str:
        executed.append(index)
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
            request_streams.append(body.get("stream", False))
            return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"),
                content="done" if turns > 32 else "",
                calls=() if turns > 32 else (ToolCall(id=f"call_{turns}", name="tool_query", arguments={"index": turns}),))
        return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"), content=content)

    async with _endpoint(protocol="chat_completions", token_counting="heuristic", context_window_tokens=4096,
                         max_output_tokens=512, responder=respond) as (provider, _):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            result, events = await invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[query],
                context=dagent.ContextPolicy(compaction_strategy=strategy, summary_max_tokens=512)), stream,
                input="Collect pages.", run_id="long", workspace_path=tmp_path / "work")
            assert_public_result(runner, result, events)
            assert result.status == "completed" and result.output_text == "done"
            assert summaries > 0 and result.conversation.summary is not None
            assert executed == list(range(1, 33))
            assert result.usage.capability_calls == 32
            assert result.usage.model_turns == turns + summaries
            assert request_streams == [stream] * turns


async def _snapshot_invoke(runner, agent, host, stream, **kwargs):
    if stream:
        events = [event async for event in runner.stream(agent, **kwargs)]
        result = events[-1].data.result
    else:
        result, events = await runner.run(agent, **kwargs), []
    host.state = result.conversation
    return result, events


def assert_released_snapshot(payload: str, fixture: Path, tmp_path: Path) -> None:
    hashes_path = fixture.with_name("response_normal_contract_hashes.json")
    hashes = json.loads(hashes_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    compressed = fixture.suffix == ".gz"
    if os.environ.get("DAGENT_CAPTURE_BASELINE") == "1":
        if compressed:
            fixture.write_bytes(gzip.compress(payload.encode(), mtime=0))
        else:
            fixture.write_text(payload, encoding="utf-8")
        hashes[fixture.name] = digest
        hashes_path.write_text(json.dumps(hashes, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    if digest != hashes[fixture.name]:
        (tmp_path / "actual.json").write_text(payload, encoding="utf-8")
        if fixture.exists():
            expected = gzip.decompress(fixture.read_bytes()).decode() if compressed else fixture.read_text(encoding="utf-8")
            (tmp_path / "expected.json").write_text(expected, encoding="utf-8")
    assert digest == hashes[fixture.name], f"{fixture.name}: output differs; see {tmp_path / 'actual.json'}"


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
async def test_released_response_snapshot(tmp_path, stable_clocks, scenario):
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
            host.state = result.conversation
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

        result, events = await _snapshot_invoke(runner, agent, host, True, input="echo", conversation=initial,
                                      run_id="baseline", workspace_path=tmp_path / "work")
        capture(result, events)
        if scenario == "review":
            events = [event async for event in runner.resume_stream(result.review.approve(), checkpoint=result.checkpoint,
                      )]
            capture(events[-1].data.result, events)
        if scenario == "failed_continue":
            result, events = await _snapshot_invoke(runner, agent, host, True, input="continue", conversation=result.conversation,
                                          run_id="continued", workspace_path=tmp_path / "work")
            capture(result, events)
    payload = json.dumps(snapshots, sort_keys=True, ensure_ascii=False, indent=2).replace(str(tmp_path), "<workspace>") + "\n"
    fixture = Path(__file__).parent / "fixtures" / f"response_baseline_{scenario}.json"
    assert_released_snapshot(payload, fixture, tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["legacy", "chunked", "cache_aware"])
async def test_released_long_context_snapshot(tmp_path, stable_clocks, strategy):
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
            result, events = await _snapshot_invoke(runner, dagent.ToolAgent(profile="conversation", capabilities=[query],
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
    assert_released_snapshot(payload, fixture, tmp_path)
