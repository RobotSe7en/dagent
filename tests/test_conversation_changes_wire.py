"""Real provider payloads and repeated compaction through the public API."""

from contextlib import closing
import json

import pytest

import dagent
from dagent.providers import ToolCall
from tests.test_cache_usage import _completion, _usage
from tests.test_compaction_strategy import checkpoint, request_aliases
from tests.test_conversation_changes import Host, invoke
from tests.test_reasoning_mapping import _endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_opt_in_and_reconstructed_continuation_keep_actual_wire_payloads(tmp_path, protocol, stream):
    all_requests = []

    @dagent.tool
    def echo(value: str) -> str:
        return value

    for enabled in [False, True]:
        calls = 0

        def respond(request):
            nonlocal calls
            calls += 1
            body = json.loads(request.content)
            return _completion(protocol, body.get("stream", False), usage=_usage(protocol),
                content="answer" if calls != 1 else "", reasoning="observed reasoning",
                calls=(ToolCall(id="call", name="tool_echo", arguments={"value": "evidence"}),) if calls == 1 else ())

        async with _endpoint(protocol=protocol, token_counting="heuristic", responder=respond) as (provider, posts):
            with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
                host = Host(runner=runner)
                agent = dagent.ToolAgent(profile="conversation", capabilities=[echo],
                                        context=dagent.ContextPolicy(reasoning_replay="all_runs"))
                arguments = dict(input="first", run_id="first", workspace_path=tmp_path / "session")
                if enabled:
                    first, _ = await invoke(runner, agent, host, stream, **arguments)
                    assert first.conversation == host.state
                    state = dagent.ConversationState.model_validate_json(host.state.model_dump_json())
                else:
                    if stream:
                        events = [event async for event in runner.stream(agent, **arguments)]
                        first = events[-1].data.result
                    else:
                        first = await runner.run(agent, **arguments)
                    state = first.conversation
                arguments = dict(input="next", run_id="next", workspace_path=tmp_path / "session", conversation=state)
                if enabled:
                    second, _ = await invoke(runner, agent, host, stream, **arguments)
                    assert host.state == second.conversation
                elif stream:
                    _ = [event async for event in runner.stream(agent, **arguments)]
                else:
                    await runner.run(agent, **arguments)
                # The SDK-returned and host-replayed copies must produce exactly
                # the same request, including generated Responses reasoning ids.
                arguments.update(conversation=first.conversation, run_id="compare")
                if stream:
                    _ = [event async for event in runner.stream(agent, **arguments)]
                else:
                    await runner.run(agent, **arguments)
                assert json.loads(posts[-1].content) == json.loads(posts[-2].content)
            payloads = [json.loads(request.content) for request in posts]
            # Independent runs have different message UUIDs. Keep identity
            # relationships across requests while comparing their wire behavior.
            identities = {}
            for body in payloads:
                for item in body.get("input", ()):
                    if item.get("type") in {"reasoning", "function_call", "message"} and "id" in item:
                        value = item["id"]
                        item["id"] = identities.setdefault(value, f"identity_{len(identities)}")
            all_requests.append(payloads)
    assert all_requests[0] == all_requests[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["legacy", "chunked", "cache_aware"])
@pytest.mark.parametrize("stream", [False, True])
async def test_repeated_compaction_and_frozen_results_are_replayable(tmp_path, strategy, stream):
    steps = 0
    summaries = 0

    @dagent.tool
    def query(index: int) -> str:
        return f"step={index}; " + "evidence " * 250

    def respond(request):
        nonlocal steps, summaries
        body = json.loads(request.content)
        messages = body["messages"]
        raw = "\n".join(str(message.get("content", "")) for message in messages)
        is_summary = "Sources to summarize:" in raw or messages[0].get("content", "").startswith("Produce a task checkpoint")
        # Legacy uses a different summary prompt, but also supplies no tools.
        is_summary = is_summary or "Compact" in messages[0].get("content", "") or "Summarize" in messages[0].get("content", "")
        if is_summary:
            summaries += 1
            ids = request_aliases(raw)
            content = (checkpoint(requirements=[("Collect evidence.", [ids[0]])],
                                  next_step=[("Continue.", [ids[-1]])]) if ids else "Earlier pages were collected.")
            return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"), content=content)
        steps += 1
        return _completion("chat_completions", body.get("stream", False), usage=_usage("chat_completions"),
            content="done" if steps > 24 else "",
            calls=() if steps > 24 else (ToolCall(id=f"call_{steps}", name="tool_query", arguments={"index": steps}),))

    async with _endpoint(protocol="chat_completions", token_counting="heuristic", context_window_tokens=8192,
                         max_output_tokens=512, responder=respond) as (provider, _):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            host = Host(runner=runner)
            agent = dagent.ToolAgent(profile="conversation", capabilities=[query, "tool.read_file"], max_steps=30,
                context=dagent.ContextPolicy(compaction_strategy=strategy, compaction_trigger_ratio=.3,
                                             summary_max_tokens=256, max_tool_result_tokens=512))
            first, _ = await invoke(runner, agent, host, stream, input="Collect evidence.", workspace_path=tmp_path / "session")
            assert first.status == "completed"
            assert host.state == first.conversation
            commits = [batch.context.summary_change for batch in host.batches
                       if batch.context is not None and batch.context.summary_change is not None]
            assert len(commits) >= 2
            assert any(commit.replaced_item_ids for commit in commits)
            if strategy != "legacy":
                assert host.resources
                assert any(record.item.model_display is not None for batch in host.batches for record in batch.messages
                           if isinstance(record.item, dagent.ToolResultMessage))
            second, _ = await invoke(runner, agent, host, stream, input="Continue.", conversation=host.state,
                                      workspace_path=tmp_path / "continued")
            assert host.state == second.conversation
            assert second.status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["chunked", "cache_aware"])
async def test_context_failure_publishes_the_actual_retained_snapshot(tmp_path, strategy):
    async with _endpoint(protocol="chat_completions", token_counting="heuristic", context_window_tokens=2048,
                         max_output_tokens=256) as (provider, posts):
        with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
            host = Host(runner=runner)
            with pytest.raises(dagent.ContextWindowExceeded) as captured:
                await runner.run(dagent.ToolAgent(profile="conversation", capabilities=[],
                    context=dagent.ContextPolicy(compaction_strategy=strategy)), input="protected input " * 5000,
                    on_conversation_change=host.save)
            assert host.state == captured.value.result.conversation
            assert host.state.summary is None
            assert posts == []
