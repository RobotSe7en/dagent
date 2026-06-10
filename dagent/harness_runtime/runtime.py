"""Top-level harness runtime.

The runtime orchestrates: route -> loop -> review -> validation -> final result.
It does not know how loops execute internally; it only consumes LoopOutcome.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any, Literal
from pathlib import Path
from uuid import uuid4

from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.harness_runtime.tool_agent import (
    ToolAgent,
    LoopEventHandler,
    TokenHandler,
)
from dagent.capabilities.catalog import CapabilityHandler
from dagent.harness_runtime.capability_executor import CapabilityExecutor
from dagent.harness_runtime.capability_scope import (
    CapabilityScope,
    DEFAULT_CAPABILITY_SCOPE,
    capability_scope_from_state,
    capability_scope_to_state,
)
from dagent.harness_runtime.dag_agent import DAGAgent
from dagent.harness_runtime.artifacts import ArtifactUpload
from dagent.harness_runtime.validator_agent import ValidatorAgent, format_validation_feedback
from dagent.harness_runtime.runtime_session import HarnessRuntimeSession
from dagent.harness_runtime.runtime_events import _dag_event_emitter
from dagent.review import ReviewLevel
from dagent.providers import ChatProvider
from dagent.result import RunResult
from dagent.schemas import (
    DAG,
    DAGSpec,
    LoopOutcome,
    CapabilityDefinition,
    RunState,
)


RuntimeMode = Literal["auto", "tool", "dag"]
_LoopExecutionMode = Literal["tool", "dag", "dag_spec"]

_ROUTE_SYSTEM_PROMPT = """\
You are a routing classifier. Given the user message, \
decide whether the request needs a multi-step DAG pipeline or can be handled by a \
single bounded tool-agent loop.

Respond with exactly one word: "dag" or "tool".

Choose "dag" ONLY when the task clearly benefits from node-level planning, \
parallelism, human review gates, resumability, or multi-agent coordination.

Choose "tool" for greetings, simple Q&A, single tool calls, ordinary short \
serial work, or anything that does not need structured orchestration.\
"""


LoopRunner = Callable[[str | None], Awaitable[LoopOutcome]]


class HarnessRuntime:
    """Orchestrates: route -> loop -> review -> validation -> final result."""

    def __init__(
        self,
        *,
        provider: ChatProvider,
        tool_agent: ToolAgent,
        dag_agent: DAGAgent,
        validator: ValidatorAgent | None = None,
        enable_validation: bool = False,
        max_validation_retries: int = 1,
        capability_catalog: CapabilityCatalog | None = None,
        capability_executor: CapabilityExecutor | None = None,
        agent_capability_configs: Iterable[MutableMapping[str, Any]] | None = None,
    ) -> None:
        self.provider = provider
        self.tool_agent = tool_agent
        self.dag_agent = dag_agent
        self.validator = validator
        self.enable_validation = enable_validation
        self.max_validation_retries = max_validation_retries
        self.session = HarnessRuntimeSession()
        self.runs = self.session.runs
        if capability_catalog is None and capability_executor is not None:
            capability_catalog = capability_executor.catalog
        self.capability_catalog = capability_catalog or CapabilityCatalog()
        if capability_executor is not None and capability_executor.catalog is not self.capability_catalog:
            raise ValueError("HarnessRuntime capability_catalog must match capability_executor.catalog.")
        self.capability_executor = capability_executor or CapabilityExecutor(self.capability_catalog)
        self._agent_capability_configs = list(agent_capability_configs or [])

    # ==================================================================
    # Public API
    # ==================================================================

    def register_capability(
        self,
        capability: Any,
        handler: CapabilityHandler | None = None,
        *,
        supports_context: bool | None = None,
    ) -> CapabilityDefinition:
        definition, resolved_handler, resolved_supports_context = _capability_registration_parts(
            capability,
            handler,
            supports_context=supports_context,
        )
        self.capability_catalog.register(
            definition,
            resolved_handler,
            supports_context=resolved_supports_context,
        )
        self.refresh_toolsets()
        return self.capability_catalog.get(definition.id) or definition

    def replace_capability(
        self,
        capability: Any,
        handler: CapabilityHandler | None = None,
        *,
        supports_context: bool | None = None,
    ) -> CapabilityDefinition:
        definition, resolved_handler, resolved_supports_context = _capability_registration_parts(
            capability,
            handler,
            supports_context=supports_context,
        )
        self.capability_catalog.replace(
            definition,
            resolved_handler,
            supports_context=resolved_supports_context,
        )
        self.refresh_toolsets()
        return self.capability_catalog.get(definition.id) or definition

    def refresh_toolsets(self) -> CapabilityToolAdapter:
        tool_adapter = CapabilityToolAdapter(
            self.capability_catalog,
            toolsets=[CapabilityToolset("builtin", tuple(sorted(self.capability_catalog.ids())))],
        )
        self.tool_agent.loop.tool_adapter = tool_adapter
        self.dag_agent.loop.tool_adapter = tool_adapter
        for config in self._agent_capability_configs:
            config["tool_adapter"] = tool_adapter
        return tool_adapter

    async def _validate_loop_outcome(
        self,
        loop_outcome: LoopOutcome,
        user_request: str,
        *,
        on_event: LoopEventHandler | None,
    ) -> tuple[bool, str | None]:
        passed = True
        feedback: str | None = None
        if not self.enable_validation or not self.validator:
            return passed, feedback
        if not loop_outcome.execution_context.strip():
            return passed, feedback
        if on_event:
            on_event({"type": "validating", "message": "Validating result quality..."})
        validation = await self.validator.validate(
            user_request=user_request,
            final_answer=loop_outcome.output_text,
            execution_context=loop_outcome.execution_context,
        )
        if validation.passed:
            if on_event:
                on_event({
                    "type": "validation_passed",
                    "passed": True,
                    "summary": validation.summary,
                    "issues": [
                        {"message": issue.message, "node_id": issue.node_id}
                        for issue in validation.issues
                    ],
                })
            return passed, feedback
        feedback = format_validation_feedback(validation)
        passed = False
        if on_event:
            on_event({
                "type": "retry",
                "reason": feedback,
                "summary": validation.summary,
                "issues": [
                    {"message": issue.message, "node_id": issue.node_id}
                    for issue in validation.issues
                ],
            })
        return passed, feedback

    async def _run_with_review_and_validation(
        self,
        user_request: str,
        *,
        run_once: LoopRunner,
        initial_loop_outcome: LoopOutcome | None = None,
        on_event: LoopEventHandler | None,
    ) -> LoopOutcome:
        feedback: str | None = None
        loop_outcome: LoopOutcome | None = initial_loop_outcome

        for _attempt in range(self.max_validation_retries + 1):
            if loop_outcome is None or feedback is not None:
                loop_outcome = await run_once(feedback)

            if loop_outcome.state.status == "awaiting_review":
                return loop_outcome

            passed, validation_feedback = await self._validate_loop_outcome(
                loop_outcome,
                user_request,
                on_event=on_event,
            )
            if passed:
                return loop_outcome
            feedback = validation_feedback

        if loop_outcome is None:
            raise RuntimeError("Validation retry loop did not execute.")
        return loop_outcome

    async def handle_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        run_state: RunState | None = None,
        mode: RuntimeMode = "auto",
        review_level: ReviewLevel = "fast",
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        user_request = _last_user_content(messages)
        loop_messages = _messages_for_run_state(run_state, user_request) if run_state else messages
        # 1. Route
        resolved_mode = mode
        if mode == "auto":
            resolved_mode = await self._route(user_request)
        run_id = _new_run_id_for_mode(resolved_mode)
        _emit_run_started(on_event, run_id=run_id, kind=_state_kind_for_mode(resolved_mode))

        async def run_once(feedback: str | None) -> LoopOutcome:
            if feedback is None:
                return await self._execute_loop(
                    user_request,
                    run_id=run_id,
                    mode=resolved_mode,
                    review_level=review_level,
                    capability_scope=capability_scope,
                    messages=loop_messages,
                    on_token=on_token,
                    on_event=on_event,
                )
            return await self._execute_loop(
                feedback,
                run_id=run_id,
                mode=resolved_mode,
                review_level=review_level,
                capability_scope=capability_scope,
                on_token=on_token,
                on_event=on_event,
            )

        outcome = await self._run_with_review_and_validation(
            user_request,
            run_once=run_once,
            on_event=on_event,
        )
        return self._finish_loop_outcome(
            outcome,
            user_request,
            resolved_mode,
            review_level,
            runtime_mode=resolved_mode,
            capability_scope=capability_scope,
            input_messages=loop_messages,
        )

    async def resume_review(
        self,
        review_id: str,
        *,
        run_state: RunState | None = None,
        dag: DAG | None = None,
        approved: bool = True,
        review_level: ReviewLevel | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult | None:
        if run_state is not None:
            self.session.save_run_state(run_state)
        state = self.session.get_review_state(review_id)
        if state is None or state.pending_review is None:
            return None
        pending_review = state.pending_review
        capability_scope = capability_scope_from_state(state.capability_scope)
        input_messages = list(state.internal_messages)
        _emit_run_started(on_event, run_id=state.run_id, kind=state.kind)
        if pending_review.kind == "capability_review":
            input_messages = _messages_before_pending_capability_call(state)
            self.tool_agent.messages = [dict(message) for message in state.internal_messages]
            self.tool_agent.trace = state.trace
            initial_outcome = await self.tool_agent.resume_review(
                state,
                approved=approved,
                on_token=on_token,
                on_event=on_event,
            )
            if initial_outcome is None:
                return None
            self.session.discard_review(review_id)
            retry_outcome = initial_outcome

            async def run_once(feedback: str | None) -> LoopOutcome:
                nonlocal retry_outcome
                if feedback is None:
                    raise RuntimeError("Tool review validation retry requires feedback.")
                retry_messages = [
                    *[dict(message) for message in retry_outcome.state.internal_messages],
                    {"role": "user", "content": feedback},
                ]
                previous_trace = retry_outcome.state.trace
                next_outcome = await self.tool_agent.run_messages(
                    retry_messages,
                    run_id=state.run_id,
                    review_level=state.review_level,
                    capability_scope=capability_scope,
                    on_token=on_token,
                    on_event=on_event,
                )
                if previous_trace is not None and next_outcome.state.trace is not None:
                    next_state = next_outcome.state.model_copy(
                        update={"trace": previous_trace.merge(next_outcome.state.trace)}
                    )
                    next_outcome = next_outcome.model_copy(update={"state": next_state})
                retry_outcome = next_outcome
                return retry_outcome

            outcome = await self._run_with_review_and_validation(
                state.user_request,
                run_once=run_once,
                initial_loop_outcome=initial_outcome,
                on_event=on_event,
            )
            return self._finish_loop_outcome(
                outcome,
                state.user_request,
                "tool",
                state.review_level,
                runtime_mode=state.runtime_mode,
                capability_scope=capability_scope,
                input_messages=input_messages,
            )

        submitted_dag = dag
        if approved and submitted_dag is None:
            submitted_dag = pending_review.proposed_dag
        if approved and submitted_dag is None:
            return None
        initial_outcome = await self.dag_agent.resume_review(
            state,
            dag=submitted_dag,
            approved=approved,
            review_level=review_level,
            on_token=on_token,
            on_event=on_event,
            on_dag=_dag_event_emitter(on_event),
        )
        if initial_outcome is None:
            return None
        self.session.discard_review(review_id)
        if initial_outcome.state.status == "awaiting_review":
            return self._finish_loop_outcome(
                initial_outcome,
                state.user_request,
                "dag",
                review_level or state.review_level,
                runtime_mode=state.runtime_mode,
                capability_scope=capability_scope,
                input_messages=input_messages,
            )

        retry_outcome = initial_outcome

        async def run_once(feedback: str | None) -> LoopOutcome:
            nonlocal retry_outcome
            if feedback is None:
                raise RuntimeError("DAG review validation retry requires feedback.")
            retry_messages = [
                *[dict(message) for message in retry_outcome.state.internal_messages],
                {"role": "user", "content": feedback},
            ]
            retry_outcome = await self.dag_agent.run_messages(
                retry_messages,
                task_id=state.run_id,
                review_level=review_level or state.review_level,
                runtime_mode=state.runtime_mode,
                capability_scope=capability_scope,
                on_token=on_token,
                on_event=on_event,
                on_dag=_dag_event_emitter(on_event),
            )
            return retry_outcome

        outcome = await self._run_with_review_and_validation(
            state.user_request,
            run_once=run_once,
            initial_loop_outcome=initial_outcome,
            on_event=on_event,
        )
        return self._finish_loop_outcome(
            outcome,
            state.user_request,
            "dag",
            review_level or state.review_level,
            runtime_mode=state.runtime_mode,
            capability_scope=capability_scope,
            input_messages=input_messages,
        )

    # ==================================================================
    # Route
    # ==================================================================

    async def _route(self, message: str) -> Literal["dag", "tool"]:
        """Lightweight LLM classifier."""
        messages = [
            {"role": "system", "content": _ROUTE_SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ]
        try:
            response = await self.provider.chat(messages)
            decision = response.content.strip().lower()
            if decision in {"dag", "tool"}:
                return decision  # type: ignore[return-value]
        except Exception:
            pass
        return "tool"

    # ==================================================================
    # Loop execution
    # ==================================================================

    async def _execute_loop(
        self,
        request: str | DAGSpec,
        *,
        run_id: str,
        mode: _LoopExecutionMode,
        review_level: ReviewLevel,
        on_token: TokenHandler | None,
        on_event: LoopEventHandler | None,
        workspace_root: str | Path = ".dagent-runs",
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        graph_input: Any = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> LoopOutcome:
        """Dispatch to the appropriate loop and return a unified LoopOutcome."""
        if mode == "dag":
            if not isinstance(request, str):
                raise TypeError("DAG message execution requires a string request.")
            if messages is not None:
                return await self.dag_agent.run_messages(
                    messages,
                    task_id=run_id,
                    review_level=review_level,
                    runtime_mode=str(mode),
                    capability_scope=capability_scope,
                    on_token=on_token,
                    on_event=on_event,
                    on_dag=_dag_event_emitter(on_event),
                )
            return await self.dag_agent.run(
                request,
                task_id=run_id,
                review_level=review_level,
                runtime_mode=str(mode),
                capability_scope=capability_scope,
                on_token=on_token,
                on_event=on_event,
                on_dag=_dag_event_emitter(on_event),
            )
        elif mode == "tool":
            if not isinstance(request, str):
                raise TypeError("Tool execution requires a string request.")
            if messages is not None:
                return await self.tool_agent.run_messages(
                    messages,
                    run_id=run_id,
                    review_level=review_level,
                    capability_scope=capability_scope,
                    on_token=on_token,
                    on_event=on_event,
                )
            return await self.tool_agent.run(
                request,
                run_id=run_id,
                review_level=review_level,
                capability_scope=capability_scope,
                on_token=on_token,
                on_event=on_event,
            )
        elif mode == "dag_spec":
            if not isinstance(request, DAGSpec):
                raise TypeError("DAGSpec execution requires a DAGSpec request.")
            return await self.dag_agent.loop.run_static(
                request,
                run_id=run_id,
                graph_input=graph_input,
                workspace_root=workspace_root,
                artifact_uploads=artifact_uploads,
                on_token=on_token,
                on_event=on_event,
                on_dag=_dag_event_emitter(on_event),
            )
        else:
            raise ValueError(f"Unknown runtime mode: {mode}")

    def _finish_loop_outcome(
        self,
        outcome: LoopOutcome,
        user_request: str,
        mode: Literal["tool", "dag"],
        review_level: ReviewLevel,
        *,
        runtime_mode: str | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        input_messages: list[dict[str, Any]] | None = None,
    ) -> RunResult:
        final_answer = (
            ""
            if outcome.state.status == "awaiting_review"
            else outcome.output_text.strip() or _fallback_output_text(outcome)
        )
        state = outcome.state.model_copy(update={
            "kind": _state_kind_for_mode(mode),
            "status": outcome.state.status,
            "input_message_count": len(input_messages or []),
            "user_request": user_request,
            "review_level": review_level,
            "runtime_mode": runtime_mode or mode,
            "capability_scope": capability_scope_to_state(capability_scope),
            "pending_review": outcome.state.pending_review,
            "pending_invocation": outcome.state.pending_invocation,
        })
        state = self.session.save_run_state(state)
        return RunResult(state=state, output_text=final_answer)

    async def run_dag_spec(
        self,
        spec: DAGSpec,
        *,
        graph_input: Any = None,
        workspace_root: str | Path = ".dagent-runs",
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        run_id = _new_run_id_for_mode("dag_spec")
        _emit_run_started(on_event, run_id=run_id, kind="static_dag")
        outcome = await self._execute_loop(
            spec,
            run_id=run_id,
            mode="dag_spec",
            review_level="fast",
            on_token=on_token,
            on_event=on_event,
            workspace_root=workspace_root,
            artifact_uploads=artifact_uploads,
            graph_input=graph_input,
        )
        state = self.session.save_run_state(outcome.state)
        if state.trace is None:
            raise RuntimeError(f"DAGSpec run '{state.run_id}' completed without a run trace.")
        return RunResult(
            state=state,
            output_text=outcome.output_text.strip() or _fallback_output_text(outcome),
        )


def _fallback_output_text(loop_outcome: LoopOutcome) -> str:
    if loop_outcome.execution_context.strip():
        return loop_outcome.execution_context.strip()
    if loop_outcome.state.status == "completed":
        return "The task completed, but no final answer was produced."
    return "The task did not complete, and no final answer was produced."


def _last_user_content(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    raise ValueError("messages must contain at least one user message.")


def _messages_for_run_state(state: RunState, user_request: str) -> list[dict[str, Any]]:
    messages = [dict(message) for message in state.internal_messages]
    messages.append({"role": "user", "content": user_request})
    return messages


def _messages_before_pending_capability_call(state: RunState) -> list[dict[str, Any]]:
    invocation = state.pending_invocation
    if invocation is None:
        return [dict(message) for message in state.internal_messages]
    index = _assistant_tool_call_index(state.internal_messages, invocation.invocation_id)
    if index is None:
        return [dict(message) for message in state.internal_messages]
    return [dict(message) for message in state.internal_messages[:index]]


def _state_kind_for_mode(mode: Literal["tool", "dag"]) -> Literal["tool", "dynamic_dag"]:
    return "tool" if mode == "tool" else "dynamic_dag"


def _new_run_id_for_mode(mode: _LoopExecutionMode) -> str:
    if mode == "tool":
        return f"tool_run_{uuid4().hex}"
    if mode == "dag_spec":
        return f"dag_run_{uuid4().hex}"
    return f"task_{uuid4().hex}"


def _emit_run_started(
    on_event: LoopEventHandler | None,
    *,
    run_id: str,
    kind: str,
) -> None:
    if on_event is not None:
        on_event({"type": "run_started", "run_id": run_id, "kind": kind})


def _assistant_tool_call_index(messages: list[dict[str, Any]], invocation_id: str) -> int | None:
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls") or []:
            if isinstance(tool_call, dict) and tool_call.get("id") == invocation_id:
                return index
    return None




def _capability_registration_parts(
    capability: Any,
    handler: CapabilityHandler | None,
    *,
    supports_context: bool | None,
) -> tuple[CapabilityDefinition, CapabilityHandler, bool]:
    if isinstance(capability, CapabilityDefinition):
        if handler is None:
            raise ValueError("handler is required when registering a CapabilityDefinition.")
        return capability, handler, bool(supports_context)

    definition = getattr(capability, "definition", None)
    resolved_handler = handler or getattr(capability, "handler", None)
    if not isinstance(definition, CapabilityDefinition) or not callable(resolved_handler):
        raise TypeError("Expected a CapabilityBinding or CapabilityDefinition with handler.")
    resolved_supports_context = (
        bool(getattr(capability, "supports_context", False))
        if supports_context is None
        else supports_context
    )
    return definition, resolved_handler, resolved_supports_context
