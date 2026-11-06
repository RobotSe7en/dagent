"""The SDK owns its conversation records: user code works on copies."""

from __future__ import annotations

import asyncio
from collections import defaultdict, namedtuple
from contextlib import closing

import pytest

import dagent
from dagent.harness_runtime.capability_executor import detach_containers
from dagent.harness_runtime.conversation_changes import ConversationChangePublisher, _state_changes
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas.conversation import ContextSummary
from tests.planner_helpers import capability_plan_response, final_answer_response


def run(coro):
    return asyncio.run(coro)


def test_detach_containers_copies_plain_containers_and_shares_everything_else():
    leaf = object()
    pair = namedtuple("Pair", "a b")([1], [2])
    counts = defaultdict(list, {"a": [1]})
    value = {"rows": [[1, 2], {"k": (3, [4])}], "leaf": leaf, "pair": pair, "counts": counts, "text": "s"}
    copied = detach_containers(value)
    assert copied == value
    assert copied is not value and copied["rows"] is not value["rows"]
    assert copied["rows"][1]["k"] is not value["rows"][1]["k"] and copied["rows"][1]["k"][1] is not value["rows"][1]["k"][1]
    # Objects that are not plain containers keep their type and identity.
    assert copied["leaf"] is leaf and copied["pair"] is pair and copied["counts"] is counts


def test_a_tool_changing_its_arguments_leaves_the_recorded_call_unchanged(tmp_path):
    seen = []

    @dagent.tool
    def append(values: list, options: dict) -> str:
        values.append("added")
        options["nested"]["flag"] = True
        seen.append((list(values), dict(options["nested"])))
        return "done"

    arguments = {"values": ["original"], "options": {"nested": {"flag": False}}}
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call", name="tool_append", arguments=arguments)]),
        ChatResponse(content="done"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = run(runner.run(dagent.ToolAgent(profile="conversation", capabilities=[append]), input="append"))
    assert seen == [(["original", "added"], {"flag": True})]
    call = next(item for item in result.conversation.items if isinstance(item, dagent.AssistantMessage)).tool_calls[0]
    assert call.arguments == {"values": ["original"], "options": {"nested": {"flag": False}}}
    sent = next(message for message in provider.requests[-1]["messages"] if message.get("tool_calls"))
    assert '"added"' not in str(sent) and "True" not in str(sent)


def test_a_dag_node_changing_its_arguments_leaves_the_planner_observation_unchanged(tmp_path):
    @dagent.tool
    def append(values: list) -> str:
        values.append("added")
        return "done"

    provider = MockProvider([
        ChatResponse(content=capability_plan_response("tool.append", {"values": ["original"]}, node_id="append")),
        ChatResponse(content=final_answer_response("done")),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = run(runner.run(dagent.DagAgent(capabilities=[append]), input="append"))
    assert result.status == "completed"
    planner = str(provider.requests[-1]["messages"])
    assert "original" in planner and "added" not in planner


def test_a_result_is_recorded_as_returned(tmp_path):
    cache = {"rows": [1]}

    @dagent.tool
    def read() -> dict:
        return cache

    @dagent.tool
    def change() -> str:
        cache["rows"].append(2)
        return "changed"

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="read", name="tool_read", arguments={})]),
        ChatResponse(tool_calls=[ToolCall(id="change", name="tool_change", arguments={})]),
        ChatResponse(content="done"),
    ])
    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = run(runner.run(dagent.ToolAgent(profile="conversation", capabilities=[read, change]), input="go"))
    recorded = next(item for item in result.conversation.items
                    if isinstance(item, dagent.ToolResultMessage) and item.call_id == "read")
    assert recorded.value == {"rows": [1]} and cache == {"rows": [1, 2]}


def _state(count, *, summary=None):
    items = tuple(dagent.UserMessage(id=f"m{i}", content=f"message {i}") for i in range(count))
    return dagent.ConversationState(id="conversation", items=items, summary=summary)


def test_publishing_does_not_deep_copy_the_conversation(monkeypatch):
    delivered = []

    async def save(batch):
        delivered.append(batch)

    initial = _state(200)
    publisher = ConversationChangePublisher(save, initial)
    publisher.bind("run", "tool", None, ".runtime", initial)
    deep_copies = []
    original = dagent.ConversationState.model_copy

    def counted(self, *args, **kwargs):
        if kwargs.get("deep"):
            deep_copies.append(len(self.items))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(dagent.ConversationState, "model_copy", counted)
    state = publisher.conversation
    for step in range(5):
        state = state.model_copy(update={"items": (*state.items, dagent.UserMessage(id=f"n{step}", content="new")),
                                         "revision": state.revision + 1})
        run(publisher.publish(state=state))
    assert deep_copies == []
    assert [batch.context.added[0].item_id for batch in delivered] == [f"n{step}" for step in range(5)]


def _reference_changes(before, after):
    """The released, comparison-only form of the change computation."""
    old = {item.id: item for item in before.items}
    updated = tuple(item.id for item in after.items if item.id in old and item != old[item.id])
    changed = tuple(item for item in after.items if old.get(item.id) != item)
    return updated, changed


def test_identity_shortcut_finds_the_same_changes_as_full_comparison():
    before = _state(50)
    items = list(before.items)
    # Replace two messages: one with equal content (a new object), one changed.
    items[3] = items[3].model_copy()
    items[7] = items[7].model_copy(update={"content": "edited"})
    items.append(dagent.UserMessage(id="added", content="added"))
    after = before.model_copy(update={"items": tuple(items), "revision": 1})
    context, changed = _state_changes(before, after)
    updated, expected = _reference_changes(before, after)
    assert context.updated_item_ids == updated == ("m7",)
    assert changed == expected
    compacted = after.model_copy(update={"items": after.items[10:], "revision": 2,
                                         "summary": ContextSummary(content="summary", source_item_count=10)})
    context, changed = _state_changes(after, compacted)
    assert changed == _reference_changes(after, compacted)[1] == ()
    assert context.summary_change.replaced_item_ids == tuple(f"m{i}" for i in range(10))


@pytest.mark.parametrize("mode", ["run", "stream"])
def test_host_changes_to_its_conversation_during_a_run_do_not_reach_the_sdk(tmp_path, mode):
    conversation = dagent.ConversationState(items=(
        dagent.UserMessage(id="task", content="Run the tool."),
        dagent.AssistantMessage(id="call", tool_calls=(dagent.ToolCallItem(
            id="c1", name="tool_noop", arguments={"values": ["original"]}),)),
        dagent.ToolResultMessage(id="result", call_id="c1", name="tool_noop", status="completed",
                                 content=dagent.schemas.conversation.inline_content("ok")),
    ))

    @dagent.tool
    def noop() -> str:
        return "ok"

    async def save(batch):
        # A host that changes the state object it passed in, mid-run.
        conversation.items[1].tool_calls[0].arguments["values"].append("host")

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="c2", name="tool_noop", arguments={})]),
        ChatResponse(content="done"),
    ])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[noop])
    options = {"input": "again", "conversation": conversation, "on_conversation_change": save}

    async def collect(runner):
        if mode == "run":
            return await runner.run(agent, **options)
        events = [event async for event in runner.stream(agent, **options)]
        return events[-1].data.result

    with closing(dagent.Runner(provider=provider, workspace=tmp_path, skill_roots=[])) as runner:
        result = run(collect(runner))
    assert result.status == "completed"
    assert conversation.items[1].tool_calls[0].arguments["values"][0] == "original"
    assert "host" in conversation.items[1].tool_calls[0].arguments["values"]
    sent = next(message for message in provider.requests[-1]["messages"]
                if message.get("tool_calls") and message["tool_calls"][0]["id"] == "c1")
    assert sent["tool_calls"][0]["function"]["arguments"] == '{"values":["original"]}'
    assert result.conversation.items[1].tool_calls[0].arguments == {"values": ["original"]}


def test_static_dag_runs_keep_their_value_handling(tmp_path, monkeypatch):
    import dagent.harness_runtime.capability_executor as capability_executor

    copies = []
    original = capability_executor.detach_containers

    def counted(value):
        copies.append(value)
        return original(value)

    monkeypatch.setattr(capability_executor, "detach_containers", counted)

    @dagent.tool
    def produce() -> dict:
        return {"rows": [1]}

    @dagent.tool
    def consume(data: dict) -> int:
        data["rows"].append(2)
        return len(data["rows"])

    graph = dagent.Dag("values")
    source = dagent.Node("source", target=produce)
    target = dagent.Node("target", target=consume, inputs={"data": source.output})
    graph.add_node(source)
    graph.add_node(target)
    graph.add_edge(source, target)
    with closing(dagent.Runner(workspace=tmp_path, provider=MockProvider([]), skill_roots=[])) as runner:
        result = run(runner.run(graph))
    assert result.status == "completed"
    # Static DAGs have no conversation record to protect: their values are not
    # copied, so value passing between nodes is exactly as released.
    assert copies == []


def test_a_failed_batch_is_a_copy():
    async def fail(batch):
        raise RuntimeError("host storage is down")

    initial = _state(2)
    publisher = ConversationChangePublisher(fail, initial)
    publisher.bind("run", "tool", None, ".runtime", initial)
    item = dagent.AssistantMessage(id="call", tool_calls=(dagent.ToolCallItem(
        id="c", name="tool_q", arguments={"values": ["original"]}),))
    state = publisher.conversation.model_copy(update={"items": (*publisher.conversation.items, item), "revision": 1})
    with pytest.raises(Exception):
        run(publisher.publish(state=state))
    failed = publisher.failure.failure.failed_batch
    failed.messages[0].item.tool_calls[0].arguments["values"].append("host")
    assert state.items[-1].tool_calls[0].arguments == {"values": ["original"]}
