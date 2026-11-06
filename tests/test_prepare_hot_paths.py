"""Per-request work in context preparation stays proportional and unchanged in result."""

from __future__ import annotations

import hashlib
import json
import math

import pytest

import dagent
import dagent.harness_runtime.context_strategy as context_strategy
import dagent.harness_runtime.result_storage as result_storage
from dagent.harness_runtime.context import ContextAssembler, HeuristicTokenCounter
from dagent.harness_runtime.result_storage import ResultStore
from dagent.schemas.conversation import AssistantMessage, ToolCallItem, ToolResultMessage, UserMessage, inline_content


def _conversation():
    return dagent.ConversationState(items=(
        UserMessage(id="task", run_id="run", content="Collect evidence."),
        AssistantMessage(id="call", run_id="run", tool_calls=(ToolCallItem(id="c", name="tool_q"),)),
        ToolResultMessage(id="result", run_id="run", call_id="c", name="tool_q", status="completed",
                          content=inline_content("evidence 证据 " * 50)),
    ))


@pytest.mark.parametrize("text", [
    "", "plain ascii", "证据 and words", "emoji \U0001F600 mixed é", "\x00\x7f\x80 controls", "😀" * 3 + "a" * 9,
])
def test_heuristic_count_is_unchanged(text):
    # The released formula, with its per-character ASCII count.
    ascii_count = sum(ord(character) < 128 for character in text)
    expected = 0 if not text else max(1, math.ceil(len(text.encode("utf-8")) / 4),
                                      math.ceil(ascii_count / 4) + len(text) - ascii_count)
    assert HeuristicTokenCounter().count_text(text) == expected


def test_source_keys_are_byte_identical_to_the_persisted_form():
    policy = dagent.ContextPolicy(compaction_strategy="cache_aware")
    item = _conversation().items[2]
    # Keys are saved in compaction_state, so their form must not change.
    view = (item.call_id, item.name, item.status, item.content.text)
    value = {"id": item.id, "view": view, "policy": policy.model_dump(), "namespace": "ns", "v": 1}
    expected = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assert context_strategy._source_key(item, policy.model_dump(), "ns") == expected


@pytest.mark.asyncio
async def test_preparation_without_failures_computes_no_source_keys(tmp_path, monkeypatch):
    calls = []
    original = context_strategy._source_key

    def counted(*args, **kwargs):
        calls.append(args[0].id)
        return original(*args, **kwargs)

    monkeypatch.setattr(context_strategy, "_source_key", counted)
    prepared = await ContextAssembler(context_window_tokens=131072, max_output_tokens=8192).prepare(
        system_message={"content": "Inspect"}, conversation=_conversation(),
        policy=dagent.ContextPolicy(compaction_strategy="cache_aware"),
        result_store=ResultStore(tmp_path, ".runtime"), enable_new_strategy=True,
    )
    assert prepared.conversation.compaction_state is None
    assert calls == []


@pytest.mark.asyncio
async def test_an_unchanged_call_is_estimated_once(tmp_path, monkeypatch):
    from types import SimpleNamespace

    estimates = []
    original = context_strategy._payload_tokens

    def counted(assembler, prepared, request):
        estimates.append(prepared)
        return original(assembler, prepared, request)

    monkeypatch.setattr(context_strategy, "_payload_tokens", counted)
    calls = []

    async def prepare_call(request, *, stream):
        calls.append(SimpleNamespace(
            request=request, effective_max_output_tokens=request.max_output_tokens,
            payload={"messages": [{"role": "user", "content": getattr(item, "content", "")} for item in request.items]},
        ))
        return calls[-1]

    assembler = ContextAssembler(context_window_tokens=131072, max_output_tokens=8192)
    assembler.request_preparer = prepare_call
    prepared = await assembler.prepare(
        system_message={"content": "Inspect"}, conversation=_conversation(),
        policy=dagent.ContextPolicy(compaction_strategy="cache_aware"),
        result_store=ResultStore(tmp_path, ".runtime"), enable_new_strategy=True,
    )
    # Without a provider count, the final estimate reuses the rough one.
    assert len(calls) == 1 and estimates == calls
    expected = math.ceil(original(assembler, calls[0], calls[0].request)
                         * (1 + dagent.ContextPolicy().token_safety_margin))
    assert prepared.usage.estimated_input_tokens == expected


def test_a_compaction_archive_is_hashed_once(tmp_path, monkeypatch):
    calls = []
    original = hashlib.sha256

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(result_storage.hashlib, "sha256", counted)
    store = ResultStore(tmp_path, ".runtime")
    text = "compacted history " * 1000
    archive = store.snapshot_text(text)
    assert len(calls) == 1
    data = text.encode("utf-8")
    assert archive.sha256 == original(data).hexdigest() and (tmp_path / archive.path).read_bytes() == data
    assert archive.path.endswith(f"{archive.sha256}.txt") and archive.byte_length == len(data)
    assert store.snapshot_text(text) == archive
