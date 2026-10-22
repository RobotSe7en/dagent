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
from tests.test_compaction_strategy import checkpoint, request_aliases


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
        if "Sources to summarize:" in raw or messages[0].get("content", "").startswith("Produce a task checkpoint"):
            summaries += 1
            ids = request_aliases(raw)
            assert ids, "Every summarizer request must provide evidence source aliases"
            content = checkpoint(requirements=[("Collect evidence and preserve progress.", [ids[0]])],
                                 operation_evidence_refs=[("Completed pages and their results remain in the source archive.", [ids[-1]])],
                                 next_step=[("Continue collecting the remaining pages.", [ids[-1]])])
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

    state = {"steps": 0, "summaries": 0, "first_failure_requests": 0}

    @dagent.tool
    def query(index: int) -> str:
        """Collect one page of evidence."""
        return f"step={index}; verified=true; next={index + 1}; " + "evidence " * 180

    def respond(request):
        body = json.loads(request.content)
        messages = body.get("messages", [])
        raw = "\n".join(str(message.get("content", "")) for message in messages)
        if "Sources to summarize:" in raw or messages[0].get("content", "").startswith("Produce a task checkpoint"):
            state["summaries"] += 1
            if outage[0]:
                state["first_failure_requests"] = state["summaries"]
                return httpx.Response(503, json={"error": {"message": "summarizer overloaded"}})
            ids = request_aliases(raw)
            content = checkpoint(requirements=[("Collect evidence.", [ids[0]])],
                                 next_step=[("Continue collecting.", [ids[-1]])])
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
                if request.purpose != "compaction" and state["summaries"]:
                    outage[0] = False
                return await original_prepare(request, stream=stream)

            provider.prepare_call = prepare
            result = await runner.run(agent, input="Collect evidence and preserve progress.")
    assert result.status == "completed"
    reasons = [usage.compaction_reason or "" for usage in result.state.context_usage]
    assert any(reason.startswith("CompactionError: Summary request failed") for reason in reasons)
    # One failed episode: the unreachable warm prefix is not retried through chunks.
    assert state["summaries"] >= 6 and state["first_failure_requests"] == 6
    # The span that failed during the outage was summarized after recovery.
    assert result.conversation.compaction_state is None
    remaining = {item.call_id for item in result.conversation.items if isinstance(item, dagent.ToolResultMessage)}
    assert not remaining & {f"call_{index}" for index in range(1, 11)}
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


@pytest.mark.asyncio
@pytest.mark.parametrize("copies_inner_ids", [False, True])
async def test_accumulating_summaries_follow_the_stated_budget_over_a_long_run(tmp_path, copies_inner_ids):
    steps = 0

    @dagent.tool
    def query(index: int) -> str:
        """Collect one page of evidence."""
        return f"step={index}; verified=true; next={index + 1}; " + "evidence " * 180

    def respond(request):
        nonlocal steps
        body = json.loads(request.content)
        messages = body.get("messages", [])
        raw = "\n".join(str(message.get("content", "")) for message in messages)
        if "Sources to summarize:" in raw or messages[0].get("content", "").startswith("Produce a task checkpoint"):
            # A faithful model keeps earlier completed work, as instructed,
            # and only compresses it when it knows the size limit.
            ids = request_aliases(raw)
            entries = [{"text": text, "source_ids": refs.split(", ")}
                       for text, refs in re.findall(
                           r"^- (Collected page from [^\n]+) \[([^\[\]]+)\]\s*$", raw, re.MULTILINE)]
            if not copies_inner_ids:
                for entry in entries:
                    entry["source_ids"] = ["S0"]
            entries.extend({"text": f"Collected page from {value}; verified=true; evidence archived.",
                            "source_ids": [value]} for value in ids if value != "S0")
            limit = re.search(r"must stay under (\d+) tokens", raw)
            kept = list(entries)
            while limit and kept and len(json.dumps(kept)) > 3 * int(limit.group(1)):
                kept.pop(0)
            content = checkpoint(requirements=[("Collect evidence.", [ids[0]])],
                                 completed_work=[(entry["text"], entry["source_ids"]) for entry in kept],
                                 next_step=[("Continue collecting.", [ids[-1]])])
            return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"), content=content)
        steps += 1
        done = steps > 120
        return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"),
                           content="done" if done else "",
                           calls=() if done else (ToolCall(id=f"call_{steps}", name="tool_query", arguments={"index": steps}),))

    async with _endpoint(protocol="chat_completions", token_counting="heuristic", context_window_tokens=8192,
                         max_output_tokens=512, responder=respond) as (provider, _posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            agent = dagent.ToolAgent(profile="conversation", capabilities=[query], max_steps=130,
                                     context=dagent.ContextPolicy(compaction_strategy="cache_aware", summary_max_tokens=1024))
            result = await runner.run(agent, input="Collect evidence and preserve progress.")
    assert result.status == "completed"
    assert steps == 121
    assert result.conversation.compaction_state is None
    assert all(usage.estimated_input_tokens <= usage.input_budget_tokens for usage in result.state.context_usage)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix_cached,expected_prefix_requests", [(50, 2), (900, None)])
async def test_cache_aware_switches_to_chunks_after_a_cold_warm_prefix(tmp_path, prefix_cached, expected_prefix_requests):
    steps = 0
    kinds = []

    @dagent.tool
    def query(index: int) -> str:
        """Collect one page of evidence."""
        return f"step={index}; verified=true; " + "evidence " * 180

    def respond(request):
        nonlocal steps
        body = json.loads(request.content)
        messages = body.get("messages", [])
        raw = "\n".join(str(message.get("content", "")) for message in messages)
        if "Sources to summarize:" in raw or messages[0].get("content", "").startswith("Produce a task checkpoint"):
            prefix = "Sources to summarize:" in raw
            kinds.append("prefix" if prefix else "chunk")
            ids = request_aliases(raw)
            content = checkpoint(next_step=[("Continue collecting.", [ids[-1]])])
            # A template that renders history differently after the summary
            # instruction reports few cached prompt tokens for the warm prefix.
            usage = _usage("chat_completions", cached=prefix_cached if prefix else 0)
            return _completion("chat_completions", body.get("stream", False), usage=usage, content=content)
        steps += 1
        done = steps > 60
        return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"),
                           content="done" if done else "",
                           calls=() if done else (ToolCall(id=f"call_{steps}", name="tool_query", arguments={"index": steps}),))

    async with _endpoint(protocol="chat_completions", token_counting="heuristic", context_window_tokens=8192,
                         max_output_tokens=512, capture_cache_usage=True, stream_include_usage=True,
                         responder=respond) as (provider, _posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            agent = dagent.ToolAgent(profile="conversation", capabilities=[query], max_steps=70,
                                     context=dagent.ContextPolicy(compaction_strategy="cache_aware", summary_max_tokens=512))
            result = await runner.run(agent, input="Collect evidence and preserve progress.")
    assert result.status == "completed"
    assert len(kinds) >= 3
    if expected_prefix_requests is None:
        assert set(kinds) == {"prefix"}
    else:
        assert kinds.count("prefix") == expected_prefix_requests and kinds[:2] == ["prefix", "prefix"]


@pytest.mark.asyncio
async def test_runs_of_one_runner_share_the_verified_resource_store(tmp_path, monkeypatch):
    from dagent.harness_runtime.conversation_resources import ConversationResourceStore

    stores = []
    original = ConversationResourceStore.persist

    def persist(self, conversation, *, workspace_path):
        stores.append(self)
        return original(self, conversation, workspace_path=workspace_path)

    monkeypatch.setattr(ConversationResourceStore, "persist", persist)

    def respond(request):
        body = json.loads(request.content)
        return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"), content="done")

    async with _endpoint(protocol="chat_completions", token_counting="heuristic", responder=respond) as (provider, _posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            agent = dagent.ToolAgent(profile="conversation", context=dagent.ContextPolicy(compaction_strategy="chunked"))
            first = await runner.run(agent, input="First.")
            await runner.run(agent, input="Second.", conversation=first.conversation)
    # Per-run runtimes keep one store, so its verified-file cache survives runs.
    assert len(stores) == 2 and stores[0] is stores[1]
