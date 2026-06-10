"""Bounded tool-using agent loop."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Sequence
from uuid import uuid4

from dagent.capabilities.toolsets import CapabilityToolAdapter
from dagent.harness_runtime.dag_builder import (
    MAX_EXECUTION_CONTEXT_CHARS,
    context_excerpt,
    strip_thinking_blocks,
)
from dagent.harness_runtime.capability_executor import CapabilityExecutionContext, CapabilityExecutor
from dagent.harness_runtime.capability_scope import (
    CapabilityScope,
    DEFAULT_CAPABILITY_SCOPE,
    capability_scope_from_state,
    capability_scope_to_state,
)
from dagent.harness_runtime.runtime_events import (
    LoopEventHandler,
    ResponseStreamContext,
    TokenHandler,
    response_token_stream,
)
from dagent.review import ReviewLevel, _review_policy
from dagent.profiles import AgentProfile
from dagent.providers import ChatProvider, ChatResponse, ToolCall
from dagent.schemas import (
    Boundary,
    LoopOutcome,
    PendingReview,
    CapabilityDefinition,
    CapabilityInvocation,
    CapabilityResult,
    RunState,
    RunTrace,
    RunTraceError,
    RunTraceNode,
)
from dagent.state import PromptBuilder, PromptRequest


MAX_TOOL_RESULT_CONTEXT_CHARS = 4000


@dataclass(frozen=True)
class ControlToolResult:
    """Result returned by a harness-level control tool."""

    content: str
    stop_reason: str | None = None
    needs_review: bool = False
    review_payload: dict[str, Any] | None = None


ControlToolHandler = Callable[[ToolCall], Awaitable[ControlToolResult]]


class ToolAgent:
    """Profile-backed tool agent facade used by the runtime."""

    def __init__(
        self,
        *,
        loop: "ToolAgentLoop",
        profile: AgentProfile,
        prompt_builder: PromptBuilder | None = None,
        max_steps: int = 8,
    ) -> None:
        self.loop = loop
        self.profile = profile
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.max_steps = max_steps
        self.tools = self.loop.available_capabilities()
        self.system_message = self._build_system_message(DEFAULT_CAPABILITY_SCOPE)
        self.messages: list[dict[str, Any]] = []
        self.trace: RunTrace | None = None

    def _build_system_message(self, capability_scope: CapabilityScope) -> dict[str, str]:
        tools = self.loop.available_capabilities(capability_scope.capability_ids)
        return self.prompt_builder.build_system_message(
            PromptRequest(
                profile=self.profile,
                task_content="",
                tools=tools,
            )
        )

    def _provider_messages(
        self,
        messages: list[dict[str, Any]],
        capability_scope: CapabilityScope,
    ) -> list[dict[str, Any]]:
        system_message = self._build_system_message(capability_scope)
        return [dict(system_message), *[dict(message) for message in messages]]

    async def run(
        self,
        message: str,
        *,
        run_id: str | None = None,
        review_level: ReviewLevel = "fast",
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> LoopOutcome:
        """Append a user turn to the tool-agent thread and run the bounded loop."""
        boundary = Boundary(mode="read_only", allowed_paths=["."])
        return await self.run_messages(
            [
                self.prompt_builder.build_user_message(
                    "{{ user_message }}",
                    {"user_message": message},
                )
            ],
            run_id=run_id,
            review_level=review_level,
            capability_scope=capability_scope,
            boundary=boundary,
            on_token=on_token,
            on_event=on_event,
        )

    async def run_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        run_id: str | None = None,
        review_level: ReviewLevel = "fast",
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        boundary: Boundary | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> LoopOutcome:
        """Run a tool-agent thread from OpenAI-compatible messages."""
        self.messages = [dict(message) for message in messages]
        return await self._continue_messages(
            self.messages,
            run_id=run_id,
            review_level=review_level,
            boundary=boundary or Boundary(mode="read_only", allowed_paths=["."]),
            capability_scope=capability_scope,
            on_token=on_token,
            on_event=on_event,
        )

    async def resume_review(
        self,
        state: RunState,
        *,
        approved: bool,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> LoopOutcome | None:
        """Resume a reviewed tool call and continue the tool-agent loop."""
        pending_review = state.pending_review
        invocation = state.pending_invocation
        if pending_review is None or pending_review.kind != "capability_review" or invocation is None:
            return None
        capability_scope = capability_scope_from_state(state.capability_scope)

        feed_content = "[DENIED] Human reviewer denied this tool call. Continue without executing it."
        result: CapabilityResult | None = None
        if approved:
            try:
                result = await self.loop.capability_executor.execute(
                    invocation,
                    context=CapabilityExecutionContext(
                        task_id=state.run_id,
                        skills=capability_scope.skills,
                    ),
                )
                feed_content = _tool_content(result)
                self.loop._emit_capability_event(
                    on_event,
                    invocation,
                    "capability_result",
                    run_id=state.run_id,
                    content=feed_content,
                )
            except Exception as exc:
                feed_content = f"[TOOL_ERROR] {type(exc).__name__}: {exc}"
                self.loop._emit_capability_event(
                    on_event,
                    invocation,
                    "capability_error",
                    run_id=state.run_id,
                    content=feed_content,
                )

        _reconcile_reviewed_trace_node(
            self.trace,
            invocation,
            approved=approved,
            result=result,
            content=feed_content,
        )
        _replace_tool_result(
            self.messages,
            tool_call_id=invocation.invocation_id,
            tool_name=self.loop.tool_adapter.function_name_for_capability(
                invocation.capability_id,
                enabled_toolsets=self.loop.enabled_toolsets,
                capability_ids=capability_scope.capability_ids,
            ),
            content=feed_content,
        )
        reviewed_trace = self.trace
        outcome = await self._continue_messages(
            self.messages,
            run_id=state.run_id,
            review_level=state.review_level,
            boundary=invocation.boundary,
            capability_scope=capability_scope,
            on_token=on_token,
            on_event=on_event,
        )
        if reviewed_trace is not None and outcome.state.trace is not None:
            next_state = outcome.state.model_copy(
                update={"trace": reviewed_trace.merge(outcome.state.trace)}
            )
            outcome = outcome.model_copy(update={"state": next_state})
            self.trace = next_state.trace
        return outcome

    async def _continue_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        run_id: str | None = None,
        review_level: ReviewLevel,
        boundary: Boundary,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> LoopOutcome:
        """Resume a tool-agent conversation from already-built messages."""
        control_tool_names = self.reviewable_tool_names(capability_scope)
        provider_messages = self._provider_messages(messages, capability_scope)
        outcome = await self.loop.run(
            "",
            run_id=run_id,
            boundary=boundary,
            max_steps=self.max_steps,
            messages=provider_messages,
            control_tool_names=control_tool_names,
            review_level=review_level,
            capability_ids=capability_scope.capability_ids,
            capability_scope=capability_scope,
            skills=capability_scope.skills,
            on_token=on_token,
            on_event=on_event,
        )
        internal_messages = _strip_system_message(outcome.state.internal_messages)
        state = outcome.state.model_copy(update={"internal_messages": internal_messages})
        outcome = outcome.model_copy(update={"state": state})
        self.messages = list(internal_messages)
        self.trace = state.trace
        return outcome

    def reviewable_tool_names(self, capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE) -> set[str]:
        return self.loop.reviewable_tool_names(capability_scope.capability_ids)


class ToolAgentLoop:
    """Runs one bounded tool-using agent loop."""

    def __init__(
        self,
        *,
        provider: ChatProvider,
        capability_executor: CapabilityExecutor,
        tool_adapter: CapabilityToolAdapter,
        enabled_toolsets: Sequence[str] = ("builtin",),
    ) -> None:
        self.provider = provider
        self.capability_executor = capability_executor
        self.tool_adapter = tool_adapter
        self.enabled_toolsets = tuple(enabled_toolsets)

    def available_capabilities(
        self,
        capability_ids: Sequence[str] | None = None,
    ) -> list[CapabilityDefinition]:
        return self.tool_adapter.capabilities(
            self.enabled_toolsets,
            capability_ids=capability_ids,
        )

    def reviewable_tool_names(self, capability_ids: Sequence[str] | None = None) -> set[str]:
        return self.tool_adapter.reviewable_names(
            self.enabled_toolsets,
            capability_ids=capability_ids,
        )

    def create_tool_guard(
        self,
        review_level: ReviewLevel,
        boundary: Boundary,
        capability_ids: Sequence[str] | None = None,
        *,
        context: CapabilityExecutionContext | None = None,
    ) -> ControlToolHandler:
        policy = _review_policy(review_level)

        async def guard(tool_call: ToolCall) -> ControlToolResult:
            definition = self.tool_adapter.definition_from_tool_call(
                tool_call,
                enabled_toolsets=self.enabled_toolsets,
                capability_ids=capability_ids,
            )
            risk = definition.policy.risk
            invocation = self.tool_adapter.invocation_from_tool_call(
                tool_call,
                boundary,
                enabled_toolsets=self.enabled_toolsets,
                capability_ids=capability_ids,
            )
            if not policy.reviews_tool(risk):
                try:
                    result_content = _tool_content(
                        await self.capability_executor.execute(
                            invocation,
                            context=context,
                        )
                    )
                except Exception as exc:
                    return ControlToolResult(
                        content=f"[TOOL_ERROR] {type(exc).__name__}: {exc}",
                    )
                return ControlToolResult(content=result_content)
            return ControlToolResult(
                content=f"[PENDING_REVIEW] Capability '{invocation.capability_id}' requires human review (risk={risk}).",
                needs_review=True,
                review_payload={"capability_id": invocation.capability_id, "risk": risk},
            )

        return guard

    async def run(
        self,
        user_message: str,
        *,
        run_id: str | None = None,
        boundary: Boundary,
        max_steps: int = 8,
        messages: list[dict[str, Any]] | None = None,
        control_tool_names: set[str] | None = None,
        review_level: ReviewLevel | None = None,
        capability_ids: Sequence[str] | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
        skills: tuple[str, ...] | None = None,
        capability_context: CapabilityExecutionContext | None = None,
    ) -> LoopOutcome:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1.")

        loop_messages = list(messages or [])
        if user_message:
            loop_messages.append({"role": "user", "content": user_message})
        resolved_run_id = run_id or f"tool_run_{uuid4().hex}"
        trace = RunTrace(run_id=resolved_run_id, root=RunTraceNode.run(run_id=resolved_run_id))
        execution_context = _execution_context(
            capability_context,
            task_id=resolved_run_id,
            skills=skills,
        )
        state_scope = _state_capability_scope(
            capability_scope,
            capability_ids=capability_ids,
            skills=skills,
        )
        control_tool_handler: ControlToolHandler | None = None
        if control_tool_names and review_level is not None:
            control_tool_handler = self.create_tool_guard(
                review_level,
                boundary,
                capability_ids,
                context=execution_context,
            )

        for step in range(1, max_steps + 1):
            tool_definitions = self._llm_tool_definitions(capability_ids=capability_ids)
            response = await self._chat(
                loop_messages,
                tools=tool_definitions,
                run_id=resolved_run_id,
                step=step,
                on_token=on_token,
                on_event=on_event,
            )

            assistant_message = self._assistant_message(response)
            loop_messages.append(assistant_message)
            trace.root.children.append(
                RunTraceNode(
                    parent_id=trace.root.id,
                    kind="model_call",
                    status="completed",
                    label=f"model_step_{step}",
                    ref={"step": str(step)},
                )
            )

            if not response.tool_calls:
                trace.root.status = "completed"
                return LoopOutcome(
                    state=_tool_run_state(
                        run_id=resolved_run_id,
                        status="completed",
                        messages=loop_messages,
                        trace=trace,
                        capability_scope=state_scope,
                    ),
                    execution_context=_format_capability_execution_context(loop_messages),
                    output_text=strip_thinking_blocks(response.content).strip(),
                )

            for tool_call in response.tool_calls:
                try:
                    invocation = self.tool_adapter.invocation_from_tool_call(
                        tool_call,
                        boundary,
                        enabled_toolsets=self.enabled_toolsets,
                        capability_ids=capability_ids,
                    )
                except KeyError as exc:
                    error_content = f"[TOOL_ERROR] {_exception_message(exc)}"
                    loop_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": error_content,
                        }
                    )
                    continue
                self._emit_capability_event(on_event, invocation, "capability_call", run_id=resolved_run_id)
                if control_tool_names and tool_call.name in control_tool_names:
                    if control_tool_handler is None:
                        raise ValueError(f"Control tool '{tool_call.name}' has no handler.")
                    try:
                        control_result = await control_tool_handler(tool_call)
                    except Exception as exc:
                        self._emit_capability_event(
                            on_event,
                            invocation,
                            "capability_error",
                            run_id=resolved_run_id,
                            content=str(exc),
                        )
                        trace.root.children.append(
                            RunTraceNode.capability_call(
                                parent_id=trace.root.id,
                                invocation=invocation,
                                result=CapabilityResult.failed(invocation, str(exc), stop_reason=type(exc).__name__),
                                error=str(exc),
                            )
                        )
                        raise
                    loop_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": control_result.content,
                        }
                    )
                    self._emit_capability_event(
                        on_event,
                        invocation,
                        "capability_result",
                        run_id=resolved_run_id,
                        content=control_result.content,
                    )
                    review_pending = control_result.needs_review
                    trace.root.children.append(
                        RunTraceNode.capability_call(
                            parent_id=trace.root.id,
                            invocation=invocation,
                            result=(
                                None
                                if review_pending
                                else CapabilityResult.completed(invocation, control_result.content)
                            ),
                            status="awaiting_review" if review_pending else None,
                        )
                    )
                    if review_pending:
                        pending_review = PendingReview(
                            review_id=f"review_{uuid4().hex}",
                            kind="capability_review",
                            message=f"Review capability call: {invocation.capability_id}",
                            capability_call={
                                "invocation_id": invocation.invocation_id,
                                "capability_id": invocation.capability_id,
                                "arguments": invocation.arguments,
                            },
                            payload=control_result.review_payload or {},
                        )
                        return LoopOutcome(
                            state=_tool_run_state(
                                run_id=resolved_run_id,
                                status="awaiting_review",
                                messages=loop_messages,
                                trace=trace,
                                pending_review=pending_review,
                                pending_invocation=invocation,
                                capability_scope=state_scope,
                            ),
                            execution_context=_format_capability_execution_context(loop_messages),
                        )
                    if control_result.stop_reason:
                        trace.root.status = "failed"
                        return LoopOutcome(
                            state=_tool_run_state(
                                run_id=resolved_run_id,
                                status="failed",
                                messages=loop_messages,
                                trace=trace,
                                capability_scope=state_scope,
                            ),
                            execution_context=_format_capability_execution_context(loop_messages),
                            output_text=control_result.content,
                        )
                    continue

                try:
                    capability_result = await self.capability_executor.execute(
                        invocation,
                        context=execution_context,
                    )
                    tool_result = _tool_content(capability_result)
                except Exception as exc:
                    error_content = f"[TOOL_ERROR] {type(exc).__name__}: {exc}"
                    self._emit_capability_event(
                        on_event,
                        invocation,
                        "capability_error",
                        run_id=resolved_run_id,
                        content=error_content,
                    )
                    loop_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": error_content,
                        }
                    )
                    trace.root.children.append(
                        RunTraceNode.capability_call(
                            parent_id=trace.root.id,
                            invocation=invocation,
                            result=CapabilityResult.failed(invocation, error_content, stop_reason=type(exc).__name__),
                            error=error_content,
                        )
                    )
                    continue
                loop_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": tool_result,
                    }
                )
                self._emit_capability_event(
                    on_event,
                    invocation,
                    "capability_result",
                    run_id=resolved_run_id,
                    content=tool_result,
                )
                trace.root.children.append(
                    RunTraceNode.capability_call(
                        parent_id=trace.root.id,
                        invocation=invocation,
                        result=capability_result,
                        error=capability_result.error,
                    )
                )

        trace.root.status = "failed"
        return LoopOutcome(
            state=_tool_run_state(
                run_id=resolved_run_id,
                status="failed",
                messages=loop_messages,
                trace=trace,
                capability_scope=state_scope,
            ),
            execution_context=_format_capability_execution_context(loop_messages),
        )

    def _llm_tool_definitions(
        self,
        *,
        capability_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.tool_adapter.definitions(
            self.enabled_toolsets,
            capability_ids=capability_ids,
        )

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        run_id: str | None,
        step: int,
        on_token: TokenHandler | None,
        on_event: LoopEventHandler | None,
    ) -> ChatResponse:
        if on_token is None and on_event is None:
            return await self.provider.chat(messages, tools=tools)

        stream = response_token_stream(
            on_raw=on_token,
            on_event=on_event,
            context=ResponseStreamContext.create(run_id=run_id, model_step=step),
        )
        if stream is None:
            return await self.provider.chat(messages, tools=tools)

        response: ChatResponse | None = None
        try:
            stream.start()
            if hasattr(self.provider, "stream_chat"):
                async for event in self.provider.stream_chat(messages, tools=tools):
                    if event.type == "token" and event.content:
                        stream(event.content)
                    elif event.type == "done":
                        response = event.response
            else:
                response = await self.provider.chat(messages, tools=tools)
        finally:
            stream.finish()
        return response or ChatResponse()

    def _assistant_message(self, response: ChatResponse) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": response.content,
        }
        if response.tool_calls:
            message["tool_calls"] = [
                self._tool_call_message(tool_call) for tool_call in response.tool_calls
            ]
        return message

    def _tool_call_message(self, tool_call: ToolCall) -> dict[str, Any]:
        return {
            "id": tool_call.id,
            "type": "function",
            "function": {
                "name": tool_call.name,
                "arguments": json.dumps(tool_call.arguments),
            },
        }

    def _emit_capability_event(
        self,
        on_event: LoopEventHandler | None,
        invocation: CapabilityInvocation,
        event_type: str,
        *,
        run_id: str,
        content: str | None = None,
    ) -> None:
        if on_event is None:
            return
        payload: dict[str, Any] = {
            "type": event_type,
            "invocation_id": invocation.invocation_id,
            "capability_id": invocation.capability_id,
            "arguments": invocation.arguments,
            "run_id": run_id,
        }
        if content is not None:
            payload["content"] = content
        on_event(payload)


def _format_capability_execution_context(messages: list[dict[str, Any]]) -> str:
    """Format ToolAgentLoop tool calls for validation and fallback output."""
    lines: list[str] = []
    for message in messages:
        if message.get("role") == "tool":
            name = message.get("name", "unknown")
            content = context_excerpt(
                str(message.get("content", "")),
                limit=MAX_TOOL_RESULT_CONTEXT_CHARS,
            )
            lines.append(f"  - {name}: {content}")

    if lines:
        return context_excerpt(
            "Tool call results:\n" + "\n".join(lines),
            limit=MAX_EXECUTION_CONTEXT_CHARS,
        )

    final_answer = _last_assistant_content(messages)
    if final_answer:
        return context_excerpt(
            f"Assistant response:\n{final_answer}",
            limit=MAX_EXECUTION_CONTEXT_CHARS,
        )
    return ""


def _replace_tool_result(
    messages: list[dict[str, Any]],
    *,
    tool_call_id: str,
    tool_name: str,
    content: str,
) -> None:
    replacement = {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": tool_name,
        "content": content,
    }
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "tool" and message.get("tool_call_id") == tool_call_id:
            messages[index] = replacement
            return
    messages.append(replacement)


def _strip_system_message(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if messages and messages[0].get("role") == "system":
        return [dict(message) for message in messages[1:]]
    return [dict(message) for message in messages]


def _tool_run_state(
    *,
    run_id: str,
    status: str,
    messages: list[dict[str, Any]],
    trace: RunTrace,
    pending_review: PendingReview | None = None,
    pending_invocation: CapabilityInvocation | None = None,
    capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
) -> RunState:
    return RunState(
        run_id=run_id,
        kind="tool",
        status=status,  # type: ignore[arg-type]
        internal_messages=[dict(message) for message in messages],
        trace=trace,
        pending_review=pending_review,
        pending_invocation=pending_invocation,
        capability_scope=capability_scope_to_state(capability_scope),
        runtime_mode="tool",
    )


def _state_capability_scope(
    capability_scope: CapabilityScope,
    *,
    capability_ids: Sequence[str] | None,
    skills: tuple[str, ...] | None,
) -> CapabilityScope:
    if capability_scope != DEFAULT_CAPABILITY_SCOPE:
        return capability_scope
    if capability_ids is None and skills is None:
        return capability_scope
    return CapabilityScope(
        capability_ids=None if capability_ids is None else tuple(capability_ids),
        skills=skills,
    )


def _tool_content(result: CapabilityResult) -> str:
    if result.status == "completed":
        return result.content
    prefix = "[BOUNDARY_VIOLATION]" if result.stop_reason == "BoundaryViolation" else "[TOOL_ERROR]"
    return f"{prefix} {result.error or result.content}"


def _exception_message(exc: Exception) -> str:
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc)


def _find_capability_node(node: RunTraceNode, invocation_id: str) -> RunTraceNode | None:
    if node.kind == "capability_call" and node.ref.get("invocation_id") == invocation_id:
        return node
    for child in node.children:
        found = _find_capability_node(child, invocation_id)
        if found is not None:
            return found
    return None


def _reconcile_reviewed_trace_node(
    trace: RunTrace | None,
    invocation: CapabilityInvocation,
    *,
    approved: bool,
    result: CapabilityResult | None,
    content: str,
) -> None:
    """Settle an awaiting-review capability node once its review is resolved."""
    if trace is None:
        return
    node = _find_capability_node(trace.root, invocation.invocation_id)
    if node is None:
        return
    node.ended_at = datetime.now(timezone.utc)
    if node.capability_execution is not None:
        node.capability_execution.result = result
    if not approved:
        node.status = "failed"
        node.error = RunTraceError(
            message="Human reviewer denied this capability call.",
            code="review_denied",
        )
    elif result is not None and result.status == "completed":
        node.status = "completed"
    else:
        node.status = "failed"
        if result is not None and result.error:
            node.error = RunTraceError(message=result.error, code=result.stop_reason)


def _execution_context(
    context: CapabilityExecutionContext | None,
    *,
    task_id: str,
    skills: tuple[str, ...] | None,
) -> CapabilityExecutionContext:
    base = context or CapabilityExecutionContext(task_id=task_id)
    return replace(base, skills=skills)


def _last_assistant_content(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("content"):
            return str(message["content"])
    return ""
