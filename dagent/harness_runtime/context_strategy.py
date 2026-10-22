"""Opt-in transactional context preparation for long ToolAgent runs.

The legacy assembler and this strategy share the same conversation projection.
Only a fully counted candidate is returned to the tool loop for transmission.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from dagent.harness_runtime.context import (
    ContextAssembler, PreparedModelContext, _atomic_groups, _context_results,
    _latest_run_id, _should_replay_reasoning, _ProjectionUsage, user_content_for_model,
)
from dagent.harness_runtime.compaction import CompactionError, CompactionTransportError
from dagent.harness_runtime.model_observation import count_prepared_call
from dagent.harness_runtime.frozen_results import (
    RecoveryAccess, freeze_result_displays, reference_frozen_result_displays,
    validate_frozen_result_recovery,
)
from dagent.harness_runtime.result_projection import ResultBudgetExceeded
from dagent.harness_runtime.result_storage import ResultStore
from dagent.providers.base import StructuredOutputFormat
from dagent.providers.model_io import ModelRequest, ModelTokenCount, PreparedModelCall, model_request_to_chat
from dagent.providers.openai_compatible import (
    ProviderCapabilityError, ProviderContextWindowError, ProviderReprepareRequired,
)
from dagent.schemas.context import ContextPolicy, ContextUsage, ContextWindowExceeded, ReasoningEffort
from dagent.schemas.conversation import (
    AssistantMessage, CompactionState, ConversationState, FailedCompaction, UserMessage,
    stored_content_text,
)


@dataclass(frozen=True)
class _Candidate:
    request: ModelRequest
    projection: _ProjectionUsage
    estimate: int
    count: ModelTokenCount | None
    call: PreparedModelCall | None
    window: int
    input_budget: int
    output: int


@dataclass
class _CompactionProgress:
    conversation: ConversationState
    failed: set[str] = field(init=False)
    failed_records: list[FailedCompaction] = field(init=False)
    staged_records: list[FailedCompaction] = field(default_factory=list)

    def __post_init__(self) -> None:
        state = self.conversation.compaction_state
        self.failed = set(state.failed_sources if state else ())
        self.failed_records = list(state.failed_records if state else ())

    def snapshot(self) -> ConversationState:
        if not (self.failed or self.failed_records or self.staged_records):
            return self.conversation
        keys = self.failed | {key for record in self.staged_records for key in record.source_keys}
        records = tuple({record.record.sha256: record for record in
                         [*self.failed_records, *self.staged_records]}.values())
        state = CompactionState(failed_sources=tuple(sorted(keys)), failed_records=records)
        return self.conversation.model_copy(update={
            "compaction_state": state, "revision": self.conversation.revision + 1,
        })


def _unavailable(exc: BaseException) -> bool:
    # Only an unavailable summarizer with no paid chunk leaves nothing to keep.
    return isinstance(exc, CompactionTransportError) and not exc.completed_chunks


def _source_key(item: Any, policy: ContextPolicy, namespace: str = "") -> str:
    # A new tail must not make a failed, unchanged old source eligible again.
    if isinstance(item, AssistantMessage):
        view = (item.content, item.refusal, [call.model_dump(mode="json") for call in item.tool_calls])
    elif isinstance(item, UserMessage):
        view = (user_content_for_model(item), [result.model_display.text if result.model_display
                else stored_content_text(result.content) for result in item.result_observations])
    else:
        view = (item.call_id, item.name, item.status, item.model_display.text if item.model_display
                else stored_content_text(item.content))
    value = {"id": item.id, "view": view, "policy": policy.model_dump(), "namespace": namespace, "v": 1}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _eligible_slices(
    conversation: ConversationState, active_run_id: str | None, *,
    request: ModelRequest, assembler: ContextAssembler, retain_tokens: int,
) -> list[tuple[int, int]]:
    groups = _atomic_groups(conversation.items, start=0, end=len(conversation.items))
    retained_start = len(groups) - 1
    views = {item.source_id: item for item in request.items}
    retained_tokens = 0
    for index in range(len(groups) - 1, -1, -1):
        start, end = groups[index]
        view = tuple(views[item.id] for item in conversation.items[start:end] if item.id in views)
        tokens = assembler.token_counter.count_request(
            model_request_to_chat(ModelRequest(instructions="", items=view), reasoning_field="reasoning"), ())
        if index < len(groups) - 1 and retained_tokens + tokens > retain_tokens:
            break
        retained_start = index
        retained_tokens += tokens
    eligible: list[tuple[int, int]] = []
    for start, end in groups[:retained_start]:
        group = conversation.items[start:end]
        # Every live user steer stays verbatim. A later steer is not superseded
        # simply because more tools ran after it.
        if any(isinstance(item, UserMessage) and item.run_id == active_run_id for item in group):
            continue
        if eligible and eligible[-1][1] == start:
            eligible[-1] = (eligible[-1][0], end)
        else:
            eligible.append((start, end))
    return eligible


async def prepare_context(
    assembler: ContextAssembler,
    *,
    system_message: dict[str, Any],
    conversation: ConversationState,
    tools: Sequence[dict[str, Any]],
    policy: ContextPolicy,
    active_run_id: str | None,
    response_format: StructuredOutputFormat | None,
    stream: bool,
    max_output_tokens: int | None,
    reasoning_effort: ReasoningEffort | None,
    purpose: str,
    result_store: ResultStore | None,
    compactor: Any,
) -> PreparedModelContext:
    progress = _CompactionProgress(conversation)
    outcome = "failed"
    try:
        prepared = await _prepare_context(
            assembler, system_message=system_message, conversation=conversation,
            tools=tools, policy=policy, active_run_id=active_run_id,
            response_format=response_format, stream=stream,
            max_output_tokens=max_output_tokens, reasoning_effort=reasoning_effort,
            purpose=purpose, result_store=result_store, compactor=compactor,
            compaction_progress=progress,
        )
        outcome = prepared.usage.compaction_method
        return prepared
    except asyncio.CancelledError:
        outcome = "canceled"
        raise
    except (ContextWindowExceeded, ProviderCapabilityError, ProviderReprepareRequired, ResultBudgetExceeded, OSError) as exc:
        # The original items remain authoritative until the final gate passes.
        # Keep every paid replacement and failed span even when a later render,
        # prepare, or storage operation fails. Cancellation bypasses this path.
        exc._conversation = progress.snapshot()
        raise
    finally:
        observer = getattr(compactor, "observer", None)
        if observer is not None:
            observer.end(outcome)


async def _prepare_context(
    assembler: ContextAssembler,
    *,
    system_message: dict[str, Any],
    conversation: ConversationState,
    tools: Sequence[dict[str, Any]],
    policy: ContextPolicy,
    active_run_id: str | None,
    response_format: StructuredOutputFormat | None,
    stream: bool,
    max_output_tokens: int | None,
    reasoning_effort: ReasoningEffort | None,
    purpose: str,
    result_store: ResultStore | None,
    compactor: Any,
    compaction_progress: _CompactionProgress,
) -> PreparedModelContext:
    working = conversation
    active_run_id = active_run_id or _latest_run_id(working.items)
    omitted: set[str] = set()
    compacted = active_compacted = 0
    method = "none"
    reason: str | None = None
    prefix_break: str | None = None
    failed = compaction_progress.failed
    failed_records = compaction_progress.failed_records
    staged_records = compaction_progress.staged_records
    failure_snapshot = compaction_progress.snapshot
    source_keys: dict[int, tuple[Any, str]] = {}
    source_namespace = ""

    def source_key(item: Any) -> str:
        identity = id(item)
        if identity not in source_keys:
            source_keys[identity] = (item, _source_key(item, policy, source_namespace))
        return source_keys[identity][1]
    access = RecoveryAccess.from_tools(result_store, tools) if result_store else None
    validate_frozen_result_recovery(working, access)
    requested_output = max_output_tokens if max_output_tokens is not None else assembler.max_output_tokens
    automatic_output = requested_output is None
    if requested_output is None:
        requested_output = min(8192, max(1, assembler.context_window_tokens // 8))

    # Freeze provisional new views at the normal per-result cap. Old views are
    # never redistributed. Old history gets its compaction opportunity first.
    unprojected = working
    new_ids = {item.id for item in _context_results(working.items) if item.model_display is None}
    if new_ids:
        if result_store is None or access is None:
            raise ValueError("The new context strategy requires result storage for tool results.")
        working = freeze_result_displays(
            unprojected, result_store=result_store, policy=policy,
            counter=assembler.token_counter, recovery_access=access,
            total_budget_tokens=2**60,
        )

    async def project(*, force_count: bool = False, frozen_call: PreparedModelCall | None = None):
        nonlocal requested_output
        readable = frozenset(item.id for item in _context_results(working.items)
                             if access and (access.can_read(item.model_display.recovery)
                                            if item.model_display else result_store.can_read(item)))
        request, projection = assembler._project(
            system_message=system_message, conversation=working, tools=tools,
            policy=policy, active_run_id=active_run_id, omitted_reasoning_ids=omitted,
            response_format=response_format, max_output_tokens=requested_output,
            reasoning_effort=reasoning_effort, purpose=purpose,
            readable_result_ids=readable, strict_context_budget=True,
        )
        prepared = None
        count = None
        if assembler.request_preparer is not None:
            prepared = frozen_call or await assembler.request_preparer(request, stream=stream)
            output = prepared.effective_max_output_tokens
            if output is None:
                raise ProviderCapabilityError("The new context strategy requires an enforced output-token limit.")
            known_limits = [value for value in (assembler.configured_context_window_tokens,
                assembler.server_max_model_len, assembler.model_context_window_tokens) if value is not None]
            known_window = min(known_limits) if known_limits else assembler.context_window_tokens
            rough = assembler.token_counter.count_text(json.dumps(prepared.payload, ensure_ascii=False))
            rough += 4 * len(request.items) + 8 * len(request.tools)
            # Render endpoints also validate length. An obviously oversized
            # planning candidate must be compacted before asking the renderer;
            # every sendable candidate still passes the final count below.
            if (force_count or math.ceil(rough * (1 + policy.token_safety_margin)) <= known_window - output) and assembler.prepared_token_counter is not None:
                try:
                    prepared, count = await count_prepared_call(
                        prepared, counter=assembler.prepared_token_counter,
                        reprepare=lambda: assembler.request_preparer(request, stream=stream),
                    )
                except ProviderContextWindowError as exc:
                    assembler.server_max_model_len = exc.max_model_len
                    window = min(value for value in (
                        assembler.configured_context_window_tokens,
                        assembler.server_max_model_len,
                        assembler.model_context_window_tokens,
                    ) if value is not None)
                    inferred_output = min(8192, max(1, window // 8))
                    if automatic_output and requested_output != inferred_output:
                        requested_output = inferred_output
                        return await project(force_count=force_count)
                    raise
            # A wrapper may change the wire output limit during counting.
            output = prepared.effective_max_output_tokens
        else:
            # Useful for standalone assembly/custom model-aware counters. The
            # ToolAgent boundary separately requires prepared-call support.
            output = request.max_output_tokens
            field = await assembler.request_reasoning_field(request, stream) if assembler.request_reasoning_field else "omit"
            if assembler.request_token_counter is not None:
                count = await assembler.request_token_counter(request, field)
        if count is not None and count.max_model_len is not None:
            assembler.server_max_model_len = count.max_model_len
        limits = [value for value in (assembler.configured_context_window_tokens,
                  assembler.server_max_model_len, assembler.model_context_window_tokens) if value is not None]
        window = min(limits) if limits else assembler.context_window_tokens
        inferred_output = min(8192, max(1, window // 8))
        if automatic_output and requested_output != inferred_output:
            requested_output = inferred_output
            return await project(force_count=force_count)
        if output is None or output >= window:
            error = ProviderCapabilityError(
                f"The effective output limit ({output}) must be smaller than the context window ({window}).")
            error._conversation = failure_snapshot()
            raise error
        # Keep the last valid resolved configuration available for a failed
        # checkpoint if a newly discovered window makes explicit output invalid.
        assembler.context_window_tokens = window
        if purpose == "generation":
            assembler.last_valid_generation_context_window_tokens = window
        if count is not None:
            raw = count.count
            exact = getattr(count, "exact", None) is True
            estimate = raw if exact else math.ceil(raw * (1 + policy.token_safety_margin))
        else:
            if prepared is not None:
                raw = assembler.token_counter.count_text(json.dumps(prepared.payload, ensure_ascii=False))
                raw += 4 * len(request.items) + 8 * len(request.tools)
            else:
                raw = assembler.token_counter.count_request(model_request_to_chat(request, reasoning_field="reasoning"), request.tools)
            estimate = math.ceil(raw * (1 + policy.token_safety_margin))
        return _Candidate(request, projection, estimate, count, prepared, window, window - output, output)

    state = await project()
    call = state.call
    fixed_payload = ({key: value for key, value in call.payload.items()
                      if key not in {"messages", "input", "stream", "stream_options"}}
                     if call is not None else {"instructions": state.request.instructions, "tools": state.request.tools})
    source_namespace = json.dumps({"fixed": fixed_payload, "base_url": getattr(call, "base_url", None),
                                  "instructions": state.request.instructions,
                                  "protocol": getattr(call, "protocol", None), "summary_contract": 1},
                                 ensure_ascii=False, sort_keys=True)

    def summary_key() -> str | None:
        if working.summary is None:
            return None
        value = (source_namespace, working.summary.content,
                 working.summary.compaction_record.sha256 if working.summary.compaction_record else None)
        return "summary:" + hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()

    def usage() -> ContextUsage:
        request, projection = state.request, state.projection
        estimate, count, prepared = state.estimate, state.count, state.call
        window, budget, output = state.window, state.input_budget, state.output
        source = getattr(count, "source", None)
        return ContextUsage(
            context_window_tokens=window, max_output_tokens=output,
            input_budget_tokens=budget,
            compaction_trigger_tokens=max(1, math.floor(budget * policy.compaction_trigger_ratio)),
            compaction_retain_tokens=max(1, math.floor(budget * policy.compaction_retain_ratio)),
            estimated_input_tokens=estimate,
            system_tokens=assembler.token_counter.count_text(request.instructions),
            schema_tokens=assembler.token_counter.count_text(json.dumps(list(tools), ensure_ascii=False)),
            summary_tokens=projection.summary_tokens, history_tokens=projection.history_tokens,
            tool_result_tokens=projection.tool_result_tokens, included_items=len(working.items),
            compacted_items=compacted, compacted_active_run_items=active_compacted,
            truncated_tool_results=projection.truncated_tool_results,
            tool_result_metadata_tokens=projection.tool_result_metadata_tokens,
            tool_result_body_tokens=projection.tool_result_body_tokens,
            source_truncated_tool_results=projection.source_truncated_tool_results,
            unrecoverable_tool_results=projection.unrecoverable_tool_results,
            estimator=count.estimator if count is not None else assembler.estimator,
            server_max_model_len=assembler.server_max_model_len,
            configured_context_limit=assembler.configured_context_window_tokens,
            model_context_window_tokens=assembler.model_context_window_tokens,
            context_window_source="configured" if assembler.configured_context_window_tokens == window else
                "server" if assembler.server_max_model_len == window else
                "model" if assembler.model_context_window_tokens == window else "fallback",
            reasoning_replay_mode=policy.reasoning_replay,
            replayed_reasoning_items=projection.replayed_reasoning_items,
            replayed_reasoning_tokens=projection.replayed_reasoning_tokens,
            omitted_reasoning_items=projection.omitted_reasoning_items,
            omitted_reasoning_tokens=projection.omitted_reasoning_tokens,
            compaction_method=method, compaction_reason=reason,
            count_precision="rendered" if source == "render" else
                "tokenized_estimate" if source == "tokenize" else "estimated",
            request_fingerprint=getattr(prepared, "fingerprint", None),
            prefix_break_reason=prefix_break,
        )

    # One episode shares the compactor's eight-call budget. Selection always
    # moves forward, and failed identical sources are not retried on every turn.
    attempts = 0
    summarizer_unavailable = False
    considered_records: set[tuple[str, str | None]] = set()
    while compactor is not None and state.estimate > math.floor(state.input_budget * policy.compaction_trigger_ratio) and attempts < 8:
        hard = state.estimate > state.input_budget
        ranges = _eligible_slices(working, active_run_id, request=state.request, assembler=assembler,
                                  retain_tokens=0 if hard else math.floor(state.input_budget * policy.compaction_retain_ratio))
        if not ranges:
            break
        selected = None
        fallback_only = False
        cached_summary = None
        reuse = getattr(compactor, "reuse_compaction", None)
        if callable(reuse):
            positions = {source_key(item): index for index, item in enumerate(working.items)}
            boundaries = set(_atomic_groups(working.items, start=0, end=len(working.items)))
            starts = {start for start, _ in boundaries}
            ends = {end for _, end in boundaries}
            for record in reversed(failed_records):
                # A later paid record can depend on an earlier replacement.
                # Retry its exact match after restoring that earlier root.
                reuse_key = (record.record.sha256, summary_key())
                if reuse_key in considered_records:
                    continue
                indices = sorted(positions[key] for key in record.source_keys if key in positions)
                if (not indices or len(indices) != len(record.source_keys)
                        or indices != list(range(indices[0], indices[-1] + 1))):
                    continue
                start, end = indices[0], indices[-1] + 1
                if start not in starts or end not in ends or not any(left <= start and end <= right for left, right in ranges):
                    continue
                considered_records.add(reuse_key)
                cached_summary = await reuse(working.summary, working.items[start:end],
                    policy.summary_max_tokens, record_reference=record.record)
                if cached_summary is not None:
                    selected = (start, end)
                    break
        for start, end in ranges if selected is None else ():
            # Split only between complete atomic exchanges. New work can be
            # summarized without repeatedly sending a failed unchanged span.
            for group_start, group_end in _atomic_groups(working.items, start=start, end=end):
                blocked = any(source_key(item) in failed for item in working.items[group_start:group_end])
                if blocked and selected is not None:
                    break
                if not blocked:
                    selected = (selected[0], group_end) if selected else (group_start, group_end)
            if selected is not None:
                break
        if selected is None:
            if hard and policy.allow_reference_fallback:
                selected, fallback_only = ranges[0], True
            else:
                break
        start, end = selected
        source_items = working.items[start:end]
        before = working
        before_state = state
        # Estimate a body goal from the fixed/protected remainder, then verify
        # the entire assembled request before committing the replacement.
        selected_ids = {item.id for item in source_items}
        selected_view = tuple(item for item in state.request.items
                              if item.source_id in selected_ids or item.source_id == "context_summary")
        source_tokens = assembler.token_counter.count_request(
            model_request_to_chat(ModelRequest(instructions="", items=selected_view), reasoning_field="reasoning"), ())
        target = math.floor(state.input_budget * 0.65)
        room = target - max(0, state.estimate - source_tokens)
        summary_goal = min(policy.summary_max_tokens, max(256, room))
        compactor.source_request = state.request
        attempts += 1
        try:
            if cached_summary is not None:
                summary = cached_summary
            elif fallback_only:
                keys = {source_key(item) for item in source_items}
                prior_record = next((record.record for record in reversed(failed_records) if set(record.source_keys) == keys), None)
                summary = await compactor.reference_fallback(working.summary, source_items, summary_goal,
                    reason="Previously failed source projection; reference continuation requested.", failed_record=prior_record)
            else:
                summary = await compactor.compact(working.summary, source_items, summary_goal, allow_fallback=hard and policy.allow_reference_fallback)
        except (CompactionError, ProviderReprepareRequired) as exc:
            # Failure preserves the original range. Cancellation is a
            # BaseException and deliberately bypasses this handler.
            reason = f"{type(exc).__name__}: {exc}"[:1000]
            if _unavailable(exc):
                # Not a verdict on this source: keep it eligible for a later
                # preparation and stop calling the summarizer in this episode.
                summarizer_unavailable = True
                break
            failed_keys = tuple(source_key(item) for item in source_items)
            failed.update(failed_keys)
            record = getattr(exc, "compaction_record", None)
            if record is not None:
                failed_records.append(FailedCompaction(source_keys=failed_keys, record=record))
            if isinstance(exc, ProviderReprepareRequired):
                raise
            if fallback_only:
                break
            continue
        working = working.model_copy(update={"summary": summary, "items": working.items[:start] + working.items[end:], "revision": working.revision + 1})
        if summary.compaction_record is not None:
            staged_records.append(FailedCompaction(source_keys=tuple(source_key(item) for item in source_items),
                                                   record=summary.compaction_record))
        state = await project()
        progress = before_state.estimate - state.estimate
        minimum_progress = max(256, math.ceil(state.input_budget * .01))
        if state.estimate > state.input_budget and progress < minimum_progress:
            working, state = before, before_state
            failed.update(source_key(item) for item in source_items)
            if summary.compaction_record is not None:
                failed_records.append(staged_records[-1])
            reason = "Summary replacement did not make sufficient budget progress."
            continue
        compacted += len(source_items)
        active_compacted += sum(item.run_id == active_run_id for item in source_items)
        method, reason = summary.method, summary.fallback_reason
        prefix_break = "compaction"
        if state.estimate <= state.input_budget and progress < minimum_progress:
            break

    # Tighten only the current root once; never re-send raw source just because
    # wrappers or the final rendered token count made its first goal too large.
    tighten = getattr(compactor, "tighten", None)
    root_key = summary_key()
    if working.summary is not None and root_key in failed and state.estimate > state.input_budget:
        reuse = getattr(compactor, "reuse_compaction", None)
        record = next((record.record for record in reversed(failed_records)
                       if record.source_keys == (root_key,)), None)
        if callable(reuse) and record is not None:
            summary = await reuse(working.summary, (), policy.summary_max_tokens,
                                  record_reference=record)
            if summary is not None:
                before, before_state = working, state
                working = working.model_copy(update={"summary": summary, "revision": working.revision + 1})
                staged_records.append(FailedCompaction(source_keys=(root_key,), record=summary.compaction_record))
                state = await project()
                if state.estimate < before_state.estimate:
                    method, reason, prefix_break = summary.method, summary.fallback_reason, "compaction"
                    root_key = summary_key()
                else:
                    working, state = before, before_state
    if (callable(tighten) and not summarizer_unavailable and working.summary is not None
            and root_key not in failed and state.estimate > state.input_budget):
        current_tokens = assembler.token_counter.count_text(working.summary.content)
        goal = current_tokens - math.ceil((state.estimate - state.input_budget) / (1 + policy.token_safety_margin)) - 64
        if 64 <= goal < current_tokens:
            before, before_state = working, state
            try:
                summary = await tighten(working.summary, min(goal, policy.summary_max_tokens),
                                        allow_fallback=policy.allow_reference_fallback)
            except (CompactionError, ProviderReprepareRequired) as exc:
                reason = f"{type(exc).__name__}: {exc}"[:1000]
                if not _unavailable(exc):
                    failed.add(root_key)
                    record = getattr(exc, "compaction_record", None)
                    if record is not None:
                        failed_records.append(FailedCompaction(source_keys=(root_key,), record=record))
                if isinstance(exc, ProviderReprepareRequired):
                    raise
            else:
                working = working.model_copy(update={"summary": summary, "revision": working.revision + 1})
                if summary.compaction_record is not None:
                    staged_records.append(FailedCompaction(source_keys=(root_key,), record=summary.compaction_record))
                state = await project()
                if state.estimate < before_state.estimate:
                    method, reason, prefix_break = summary.method, summary.fallback_reason, "compaction"
                else:
                    working, state = before, before_state
                    failed.add(root_key)
                    if summary.compaction_record is not None:
                        failed_records.append(staged_records[-1])

    # Preserve the original prefix for summary reuse first. Only unresolved
    # hard pressure sheds replayable reasoning from the ephemeral projection.
    reasoning = [item for item in working.items if isinstance(item, AssistantMessage)
                 and _should_replay_reasoning(item, mode=policy.reasoning_replay, active_run_id=active_run_id)]
    for item in reasoning[:-1]:
        if state.estimate <= state.input_budget:
            break
        omitted.add(item.id)
        state = await project()

    # If the latest newly received batch still does not fit, adjust only its
    # provisional displays. A displayed historical batch stays byte-identical.
    if state.estimate > state.input_budget and new_ids and result_store and access:
        baseline = working
        original_by_id = {item.id: item for item in unprojected.items}
        originals = tuple(original_by_id.get(item.id, item) if item.id in original_by_id and (
            item.id in new_ids or isinstance(item, UserMessage) and any(f"{item.id}/{obs.id}" in new_ids for obs in item.result_observations)
        ) else item for item in working.items)
        unfrozen = working.model_copy(update={"items": originals})
        local_budget = sum(assembler.token_counter.count_text(item.model_display.text) for item in _context_results(working.items) if item.id in new_ids and item.model_display)
        for _ in range(8):
            local_budget = max(0, min(local_budget - 1, local_budget - math.ceil((state.estimate - state.input_budget) / (1 + policy.token_safety_margin))))
            try:
                working = freeze_result_displays(unfrozen, result_store=result_store, policy=policy,
                    counter=assembler.token_counter, recovery_access=access, total_budget_tokens=local_budget)
            except ResultBudgetExceeded as exc:
                # An aggressive estimate may cross the metadata floor even
                # though that exact floor still fits the final wire request.
                minimum = exc.minimum_tokens
                if minimum is not None:
                    working = freeze_result_displays(unfrozen, result_store=result_store, policy=policy,
                        counter=assembler.token_counter, recovery_access=access, total_budget_tokens=minimum)
                else:
                    working = baseline
                state = await project()
                break
            state = await project()
            if state.estimate <= state.input_budget:
                break
            if not local_budget:
                break

    if state.estimate > state.input_budget and result_store and access:
        groups = _atomic_groups(working.items, start=0, end=len(working.items))
        latest = _context_results(working.items[groups[-1][0]:]) if groups else []
        emergency_ids = {item.id for item in latest if item.model_display and item.model_display.revision == 0 and item.id not in new_ids}
        if emergency_ids:
            try:
                working = reference_frozen_result_displays(working, result_store=result_store,
                    recovery_access=access, result_ids=emergency_ids)
                state = await project()
                prefix_break = "emergency_result_reference"
            except ResultBudgetExceeded:
                pass

    # A local estimate can overcount highly compressible protected input. Ask
    # the available renderer once before declaring that final candidate too big,
    # using the exact same frozen call that will be dispatched if it fits.
    if state.estimate > state.input_budget and state.count is None and state.call is not None and assembler.prepared_token_counter is not None:
        state = await project(force_count=True, frozen_call=state.call)

    # Persist only markers that still refer to retained source items.
    retained_failed = tuple(source_key(item) for item in working.items if source_key(item) in failed)
    if summary_key() in failed:
        retained_failed += (summary_key(),)
    retained_records = tuple(record for record in failed_records if set(record.source_keys) & set(retained_failed))
    compaction_state = CompactionState(failed_sources=retained_failed, failed_records=retained_records) if retained_failed else None
    if compaction_state != working.compaction_state:
        working = working.model_copy(update={"compaction_state": compaction_state, "revision": working.revision + 1})
    final_usage = usage()
    if state.estimate > state.input_budget:
        error = ContextWindowExceeded(
            f"Model input requires approximately {state.estimate} tokens; the effective input budget is {state.input_budget}. Retained source data is available in the failed run snapshot.", usage=final_usage)
        # No staged replacement is committed when the final request cannot fit.
        error._conversation = failure_snapshot()
        raise error
    return PreparedModelContext(request=state.request, conversation=working, usage=final_usage, prepared_call=state.call)
