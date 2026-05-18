"""Bounded tool-using agent loop."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence
from uuid import uuid4

from dagent.capabilities.toolsets import CapabilityToolAdapter
from dagent.harness_runtime.dag_builder import strip_thinking_blocks
from dagent.harness_runtime.review_policy import review_policy
from dagent.harness_runtime.capability_executor import CapabilityExecutor
from dagent.harness_runtime.task_record import ReviewContinuation
from dagent.profiles import AgentProfile
from dagent.providers import ChatProvider, ChatResponse, ToolCall
from dagent.schemas import Boundary, LoopOutcome, PendingReview, CapabilityDefinition, CapabilityInvocation, CapabilityResult
from dagent.state import PromptBuilder, PromptRequest
import dagent.harness_runtime.review_policy as _rp


MAX_EXECUTION_CONTEXT_CHARS = 16000
MAX_TOOL_RESULT_CONTEXT_CHARS = 4000


@dataclass(frozen=True)
class ControlToolResult:
    """Result returned by a harness-level control tool."""

    content: str
    stop_reason: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    needs_review: bool = False
    review_payload: dict[str, Any] | None = None


ControlToolHandler = Callable[[ToolCall], Awaitable[ControlToolResult]]
TokenHandler = Callable[[str], None]
LoopEventHandler = Callable[[dict[str, Any]], None]


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
        self.tools = self.loop.available_capabilities()
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.max_steps = max_steps
        self.system_message = self.prompt_builder.build_system_message(
            PromptRequest(
                profile=self.profile,
                task_content="",
                tools=self.tools,
                memory=self.profile.memory,
            )
        )
        self.messages: list[dict[str, Any]] = [dict(self.system_message)]

    async def run(
        self,
        message: str,
        *,
        review_level: "_rp.ReviewLevel" = "fast",
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> LoopOutcome:
        """Append a user turn to the tool-agent thread and run the bounded loop."""
        boundary = Boundary(mode="read_only", allowed_paths=["."])
        self.messages.append(
            self.prompt_builder.build_user_message(
                "{{ user_message }}",
                {"user_message": message},
            )
        )
        return await self._continue_messages(
            self.messages,
            review_level=review_level,
            boundary=boundary,
            on_token=on_token,
            on_event=on_event,
        )

    async def resume_review(
        self,
        state: ReviewContinuation,
        *,
        approved: bool,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> LoopOutcome | None:
        """Resume a reviewed tool call and continue the tool-agent loop."""
        invocation = state.pending_invocation
        if state.kind != "capability_review" or invocation is None:
            return None

        feed_content = "[DENIED] Human reviewer denied this tool call. Continue without executing it."
        if approved:
            try:
                result = await self.loop.capability_executor.execute(invocation)
                feed_content = _tool_content(result)
            except Exception as exc:
                feed_content = f"[TOOL_ERROR] {type(exc).__name__}: {exc}"

        _replace_tool_result(
            self.messages,
            tool_call_id=invocation.invocation_id,
            tool_name=self.loop.tool_adapter.function_name_for_capability(
                invocation.capability_id,
                enabled_toolsets=self.loop.enabled_toolsets,
            ),
            content=feed_content,
        )
        return await self._continue_messages(
            self.messages,
            review_level=state.review_level,
            boundary=invocation.boundary,
            on_token=on_token,
            on_event=on_event,
        )

    async def _continue_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        review_level: "_rp.ReviewLevel",
        boundary: Boundary,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> LoopOutcome:
        """Resume a tool-agent conversation from already-built messages."""
        control_tool_names = self.reviewable_tool_names()
        control_tool_handler = (
            self.loop.create_tool_guard(review_level, boundary)
            if control_tool_names
            else None
        )
        outcome = await self.loop.run(
            "",
            boundary=boundary,
            max_steps=self.max_steps,
            messages=messages,
            control_tool_names=control_tool_names,
            control_tool_handler=control_tool_handler,
            on_token=on_token,
            on_event=on_event,
        )
        self.messages = list(outcome.messages)
        return outcome

    def reviewable_tool_names(self) -> set[str]:
        return self.loop.reviewable_tool_names()


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

    def available_capabilities(self) -> list[CapabilityDefinition]:
        return self.tool_adapter.capabilities(self.enabled_toolsets)

    def reviewable_tool_names(self) -> set[str]:
        return self.tool_adapter.reviewable_names(self.enabled_toolsets)

    def create_tool_guard(
        self,
        review_level: "_rp.ReviewLevel",
        boundary: Boundary,
    ) -> ControlToolHandler:
        policy = review_policy(review_level)

        async def guard(tool_call: ToolCall) -> ControlToolResult:
            definition = self.tool_adapter.definition_from_tool_call(
                tool_call,
                enabled_toolsets=self.enabled_toolsets,
            )
            risk = definition.policy.risk
            invocation = self.tool_adapter.invocation_from_tool_call(
                tool_call,
                boundary,
                enabled_toolsets=self.enabled_toolsets,
            )
            if not policy.reviews_tool(risk):
                try:
                    result_content = _tool_content(
                        await self.capability_executor.execute(invocation)
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
        boundary: Boundary,
        max_steps: int = 8,
        allowed_tools: list[str] | None = None,
        messages: list[dict[str, Any]] | None = None,
        control_tool_names: set[str] | None = None,
        control_tool_handler: ControlToolHandler | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> LoopOutcome:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1.")

        loop_messages = list(messages or [])
        if user_message:
            loop_messages.append({"role": "user", "content": user_message})
        control_events: list[dict[str, Any]] = []
        invocations: list[CapabilityInvocation] = []

        for step in range(1, max_steps + 1):
            tool_definitions = self._llm_tool_definitions(allowed_tools)
            response = await self._chat(
                loop_messages,
                tools=tool_definitions,
                on_token=on_token,
            )

            assistant_message = self._assistant_message(response)
            loop_messages.append(assistant_message)

            if not response.tool_calls:
                return LoopOutcome(
                    status="completed",
                    execution_context=_format_capability_execution_context(loop_messages),
                    final_answer=strip_thinking_blocks(response.content).strip(),
                    messages=loop_messages,
                    events=control_events,
                    invocations=invocations,
                )

            for tool_call in response.tool_calls:
                invocation = self.tool_adapter.invocation_from_tool_call(
                    tool_call,
                    boundary,
                    enabled_toolsets=self.enabled_toolsets,
                )
                invocations.append(invocation)
                self._emit_capability_event(on_event, invocation, "capability_call")
                if control_tool_names and tool_call.name in control_tool_names:
                    if control_tool_handler is None:
                        raise ValueError(f"Control tool '{tool_call.name}' has no handler.")
                    try:
                        control_result = await control_tool_handler(tool_call)
                    except Exception as exc:
                        self._emit_capability_event(on_event, invocation, "capability_error", content=str(exc))
                        raise
                    control_events.extend(control_result.events)
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
                        content=control_result.content,
                    )
                    if control_result.needs_review:
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
                            status="awaiting_review",
                            execution_context=_format_capability_execution_context(loop_messages),
                            messages=loop_messages,
                            events=control_events,
                            invocations=invocations,
                            pending_review=pending_review,
                        )
                    if control_result.stop_reason:
                        return LoopOutcome(
                            status="failed",
                            execution_context=_format_capability_execution_context(loop_messages),
                            final_answer=control_result.content,
                            messages=loop_messages,
                            events=control_events,
                            invocations=invocations,
                        )
                    continue

                if allowed_tools is not None and tool_call.name not in allowed_tools:
                    error_content = f"[ERROR] Tool '{tool_call.name}' is not allowed. Available tools: {', '.join(allowed_tools)}."
                    self._emit_capability_event(on_event, invocation, "capability_error", content=error_content)
                    loop_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": error_content,
                        }
                    )
                    continue
                try:
                    tool_result = _tool_content(
                        await self.capability_executor.execute(invocation)
                    )
                except Exception as exc:
                    error_content = f"[TOOL_ERROR] {type(exc).__name__}: {exc}"
                    self._emit_capability_event(on_event, invocation, "capability_error", content=error_content)
                    loop_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "name": tool_call.name,
                            "content": error_content,
                        }
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
                self._emit_capability_event(on_event, invocation, "capability_result", content=tool_result)

        return LoopOutcome(
            status="failed",
            execution_context=_format_capability_execution_context(loop_messages),
            messages=loop_messages,
            events=control_events,
            invocations=invocations,
        )

    def _llm_tool_definitions(
        self,
        allowed_tools: list[str] | None,
    ) -> list[dict[str, Any]]:
        allowed = set(allowed_tools) if allowed_tools is not None else None
        definitions = self.tool_adapter.definitions(self.enabled_toolsets)
        if allowed is None:
            return definitions
        return [
            definition
            for definition in definitions
            if definition["function"]["name"] in allowed
        ]

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        on_token: TokenHandler | None,
    ) -> ChatResponse:
        if on_token is None or not hasattr(self.provider, "stream_chat"):
            return await self.provider.chat(messages, tools=tools)

        response: ChatResponse | None = None
        async for event in self.provider.stream_chat(messages, tools=tools):
            if event.type == "token" and event.content:
                on_token(event.content)
            elif event.type == "done":
                response = event.response
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
        content: str | None = None,
    ) -> None:
        if on_event is None:
            return
        payload: dict[str, Any] = {
            "type": event_type,
            "invocation_id": invocation.invocation_id,
            "capability_id": invocation.capability_id,
            "arguments": invocation.arguments,
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
            content = _context_excerpt(
                str(message.get("content", "")),
                limit=MAX_TOOL_RESULT_CONTEXT_CHARS,
            )
            lines.append(f"  - {name}: {content}")

    if lines:
        return _context_excerpt(
            "Tool call results:\n" + "\n".join(lines),
            limit=MAX_EXECUTION_CONTEXT_CHARS,
        )

    final_answer = _last_assistant_content(messages)
    if final_answer:
        return _context_excerpt(
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


def _tool_content(result: CapabilityResult) -> str:
    if result.status == "completed":
        return result.content
    prefix = "[BOUNDARY_VIOLATION]" if result.stop_reason == "BoundaryViolation" else "[TOOL_ERROR]"
    return f"{prefix} {result.error or result.content}"


def _last_assistant_content(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("content"):
            return str(message["content"])
    return ""


def _context_excerpt(text: str, *, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[TRUNCATED after {limit} chars]"
