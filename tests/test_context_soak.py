"""Offline endurance checks for the opt-in ToolAgent context strategy."""

from contextlib import closing
import json
import re

import httpx
import pytest

import dagent
from dagent.harness_runtime.compaction import CompactionRecordV1
from dagent.harness_runtime.result_storage import ResultStore
from dagent.harness_runtime.context import ContextAssembler
from dagent.harness_runtime.llm_retry import LLMRetryPolicy
from dagent.providers import ToolCall
from dagent.providers.model_io import ModelTokenCount, ModelToolResultInput
from dagent.schemas.conversation import inline_content
from tests.test_cache_usage import _completion, _usage
from tests.test_reasoning_mapping import _endpoint


READ_TOOL = {"type": "function", "function": {
    "name": "tool_read_file", "parameters": {"type": "object", "properties": {
        "path": {"type": "string"}, "offset_chars": {"type": "integer"},
        "limit_chars": {"type": "integer"},
    }},
}}


def latest_result(content):
    return dagent.ConversationState(items=(
        dagent.UserMessage(id="task", run_id="run", content="Keep collecting evidence."),
        dagent.AssistantMessage(id="assistant", run_id="run", tool_calls=(dagent.ToolCallItem(id="call", name="query"),)),
        dagent.ToolResultMessage(id="result", run_id="run", call_id="call", name="query",
                                 status="completed", content=inline_content(content)),
    ))


@pytest.mark.asyncio
async def test_latest_frozen_batch_can_make_one_recoverable_emergency_transition(tmp_path):
    store = ResultStore(tmp_path, ".runtime")
    policy = dagent.ContextPolicy(compaction_strategy="chunked", max_tool_result_tokens=8192)
    source = latest_result("evidence " * 3500)
    first = await ContextAssembler(context_window_tokens=16384, max_output_tokens=512).prepare(
        system_message={"content": "Inspect evidence."}, conversation=source, tools=[READ_TOOL],
        policy=policy, result_store=store, enable_new_strategy=True)
    old = first.conversation.items[-1].model_display
    assert old.revision == 0 and old.displayed_spans
    second = await ContextAssembler(context_window_tokens=4096, max_output_tokens=512).prepare(
        system_message={"content": "Inspect evidence."}, conversation=first.conversation, tools=[READ_TOOL],
        policy=policy, result_store=store, enable_new_strategy=True)
    current = second.conversation.items[-1].model_display
    assert current.revision == 1 and current.displayed_spans == ()
    assert store.read_reference(current.previous_display).decode() == old.text
    assert store.read_reference(current.recovery).decode() == source.items[-1].content.text
    assert second.usage.prefix_break_reason == "emergency_result_reference"
    assert second.usage.estimated_input_tokens <= second.usage.input_budget_tokens
    assert [item.id for item in second.conversation.items] == [item.id for item in source.items]
    third = await ContextAssembler(context_window_tokens=4096, max_output_tokens=512).prepare(
        system_message={"content": "Inspect evidence."}, conversation=second.conversation, tools=[READ_TOOL],
        policy=policy, result_store=store, enable_new_strategy=True)
    assert third.conversation.items[-1].model_display == current


@pytest.mark.asyncio
async def test_new_batch_reference_floor_is_verified_before_declaring_overflow(tmp_path):
    async def count(request, _field):
        has_body = any(isinstance(item, ModelToolResultInput) and "Ω" in item.content for item in request.items)
        # An adversarial but consistent model-specific count makes every body
        # excerpt too expensive while the exact reference-only floor fits.
        return ModelTokenCount(count=5000 if has_body else 3000, max_model_len=4096, exact=True)

    store = ResultStore(tmp_path, ".runtime")
    policy = dagent.ContextPolicy(compaction_strategy="chunked")
    source = latest_result("Ω" * 12000)
    prepared = await ContextAssembler(context_window_tokens=4096, max_output_tokens=512,
                                      request_token_counter=count).prepare(
        system_message={"content": "Inspect evidence."}, conversation=source, tools=[READ_TOOL],
        policy=policy, result_store=store, enable_new_strategy=True)
    display = prepared.conversation.items[-1].model_display
    assert display.displayed_spans == ()
    assert prepared.usage.estimated_input_tokens == 3000
    assert prepared.usage.estimated_input_tokens <= prepared.usage.input_budget_tokens
    assert store.read_reference(display.recovery).decode() == source.items[-1].content.text


@pytest.mark.asyncio
async def test_two_hundred_tool_steps_preserve_prefix_between_compactions_and_continue(tmp_path):
    steps = 0
    summaries = 0
    observed_compactions = 0
    previous_messages = None
    executions = []

    @dagent.tool
    def query(index: int) -> str:
        """Collect one page of evidence."""
        executions.append(index)
        return f"step={index}; verified=true; next={index + 1}; " + "evidence " * 180

    def respond(request):
        nonlocal steps, summaries, observed_compactions, previous_messages
        body = json.loads(request.content)
        messages = body.get("messages", [])
        raw = "\n".join(str(message.get("content", "")) for message in messages)
        if "Eligible source IDs:" in raw or messages[0].get("content", "").startswith("Produce a task checkpoint"):
            summaries += 1
            match = re.search(r"Eligible source IDs: (\[[^\n]+\])", raw)
            ids = json.loads(match.group(1)) if match else re.findall(r"\[Source ([^;]+);", raw)
            if not ids:
                match = re.search(r'"source_ids": \["([^"]+)"', raw)
                ids = [match.group(1)] if match else []
            assert ids, "Every summarizer request must provide actual evidence source IDs"
            content = json.dumps({
                "requirements": [{"text": "Collect evidence and preserve progress.", "source_ids": [ids[0]]}],
                "operation_evidence_refs": [{"text": "Completed pages and their results remain in the source archive.", "source_ids": [ids[-1]]}],
                "next_step": [{"text": "Continue collecting the remaining pages.", "source_ids": [ids[-1]]}],
            })
            return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"), content=content)
        if previous_messages is not None and observed_compactions == summaries:
            assert messages[:len(previous_messages)] == previous_messages
        previous_messages = messages
        observed_compactions = summaries
        steps += 1
        return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"),
                           content="done" if steps > 200 else "",
                           calls=() if steps > 200 else (ToolCall(id=f"call_{steps}", name="tool_query", arguments={"index": steps}),))

    async with _endpoint(protocol="chat_completions", token_counting="heuristic", context_window_tokens=8192,
                         max_output_tokens=512, responder=respond) as (provider, _posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            agent = dagent.ToolAgent(profile="conversation", capabilities=[query], max_steps=210,
                                    context=dagent.ContextPolicy(compaction_strategy="cache_aware", summary_max_tokens=512))
            first_workspace = tmp_path / "first"
            result = await runner.run(agent, input="Collect evidence and preserve progress.", workspace_path=first_workspace)
            assert result.status == "completed"
            assert executions == list(range(1, 201))
            assert summaries >= 5
            commits = [usage for usage in result.state.context_usage if usage.compacted_items]
            assert len(commits) >= 5
            assert all(usage.estimated_input_tokens <= usage.input_budget_tokens for usage in result.state.context_usage)
            store = ResultStore(first_workspace, ".dagent")
            record = CompactionRecordV1.model_validate_json(store.read_reference(result.conversation.summary.compaction_record))
            assert record.status == "complete"
            assert record.unresolved_ranges == ()
            second_workspace = tmp_path / "second"
            previous_messages = None  # A new run changes active reasoning policy legitimately.
            continued = await runner.run(agent, input="Continue.", conversation=result.conversation, workspace_path=second_workspace)
            assert continued.status == "completed"
            assert (second_workspace / result.conversation.summary.compaction_record.path).is_file()
            assert executions == list(range(1, 201))


def _outage_agent_endpoint(outage, steps_before_done):
    """A synthetic deployment whose summarizer fails while ``outage[0]`` is set."""

    state = {"steps": 0, "summaries": 0}

    @dagent.tool
    def query(index: int) -> str:
        """Collect one page of evidence."""
        return f"step={index}; verified=true; next={index + 1}; " + "evidence " * 180

    def respond(request):
        body = json.loads(request.content)
        messages = body.get("messages", [])
        raw = "\n".join(str(message.get("content", "")) for message in messages)
        if "Eligible source IDs:" in raw or messages[0].get("content", "").startswith("Produce a task checkpoint"):
            state["summaries"] += 1
            if outage[0]:
                return httpx.Response(503, json={"error": {"message": "summarizer overloaded"}})
            match = re.search(r"Eligible source IDs: (\[[^\n]+\])", raw)
            ids = (json.loads(match.group(1)) if match else re.findall(r"\[Source ([^;]+);", raw)
                   or re.findall(r'"source_ids": \["([^"]+)"', raw))
            content = json.dumps({"requirements": [{"text": "Collect evidence.", "source_ids": [ids[0]]}],
                                  "next_step": [{"text": "Continue collecting.", "source_ids": [ids[-1]]}]})
            return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"), content=content)
        state["steps"] += 1
        done = state["steps"] >= steps_before_done
        return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"),
                           content="done" if done else "",
                           calls=() if done else (ToolCall(id=f"call_{state['steps']}", name="tool_query",
                                                           arguments={"index": state["steps"]}),))

    agent = dagent.ToolAgent(profile="conversation", capabilities=[query], max_steps=90,
                             context=dagent.ContextPolicy(compaction_strategy="cache_aware", summary_max_tokens=512))
    return agent, respond, state


@pytest.mark.asyncio
async def test_summarizer_outage_does_not_permanently_block_history(tmp_path, monkeypatch):
    monkeypatch.setattr(LLMRetryPolicy, "delay_for_retry", lambda self, index: 0.0)
    outage = [True]
    agent, respond, state = _outage_agent_endpoint(outage, steps_before_done=40)
    async with _endpoint(protocol="chat_completions", token_counting="heuristic", context_window_tokens=8192,
                         max_output_tokens=512, responder=respond) as (provider, _posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            original_prepare = provider.prepare_call

            async def prepare(request, *, stream):
                # The outage ends after the first failed maintenance episode.
                if request.purpose != "compaction" and state["summaries"] >= 12:
                    outage[0] = False
                return await original_prepare(request, stream=stream)

            provider.prepare_call = prepare
            result = await runner.run(agent, input="Collect evidence and preserve progress.")
    assert result.status == "completed"
    reasons = [usage.compaction_reason or "" for usage in result.state.context_usage]
    assert any(reason.startswith("CompactionTransportError") for reason in reasons)
    # The span that failed during the outage was summarized after recovery.
    assert result.conversation.compaction_state is None
    assert len(result.conversation.items) < 12
    assert all(usage.estimated_input_tokens <= usage.input_budget_tokens for usage in result.state.context_usage)


@pytest.mark.asyncio
async def test_conversation_failed_during_summarizer_outage_continues_after_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(LLMRetryPolicy, "delay_for_retry", lambda self, index: 0.0)
    outage = [True]
    agent, respond, state = _outage_agent_endpoint(outage, steps_before_done=10**9)
    async with _endpoint(protocol="chat_completions", token_counting="heuristic", context_window_tokens=8192,
                         max_output_tokens=512, responder=respond) as (provider, _posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            with pytest.raises(dagent.ContextWindowExceeded) as failed:
                await runner.run(agent, input="Collect evidence and preserve progress.")
            conversation = failed.value.result.conversation
            assert conversation.compaction_state is None
            outage[0] = False
            state["steps"] = 10**9  # The model now finishes on its next generation.
            continued = await runner.run(agent, input="Continue from the saved progress.", conversation=conversation)
    assert continued.output_text == "done"
    assert continued.conversation.summary is not None
