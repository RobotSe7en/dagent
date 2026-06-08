"""Top-level harness runtime.

The runtime orchestrates: route -> loop -> review -> validation -> final result.
It does not know how loops execute internally; it only consumes LoopOutcome.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from typing import Any, Literal
from pathlib import Path

from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.harness_runtime.tool_agent import (
    ToolAgent,
    LoopEventHandler,
    TokenHandler,
)
from dagent.capabilities.catalog import CapabilityHandler
from dagent.harness_runtime.capability_executor import CapabilityExecutor
from dagent.harness_runtime.capability_scope import CapabilityScope, DEFAULT_CAPABILITY_SCOPE
from dagent.harness_runtime.dag_agent import DAGAgent
from dagent.harness_runtime.artifacts import ArtifactUpload
from dagent.harness_runtime.validator_agent import ValidatorAgent, format_validation_feedback
from dagent.harness_runtime.runtime_session import HarnessRuntimeSession
from dagent.harness_runtime.runtime_events import (
    _ResponseTokenSplitter,
    _dag_event_emitter,
)
from dagent.review import ReviewLevel
from dagent.providers import ChatProvider
from dagent.schemas import (
    DAG,
    DAGRun,
    DAGSpec,
    LoopOutcome,
    RuntimeResponse,
    CapabilityInvocation,
    CapabilityDefinition,
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
        self.tasks = self.session.tasks
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
            final_answer=loop_outcome.final_answer,
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

            if loop_outcome.status == "awaiting_review":
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

    async def handle_message(
        self,
        message: str,
        *,
        mode: RuntimeMode = "auto",
        review_level: ReviewLevel = "fast",
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RuntimeResponse:
        # 1. Route
        resolved_mode = mode
        if mode == "auto":
            resolved_mode = await self._route(message)

        async def run_once(feedback: str | None) -> LoopOutcome:
            return await self._execute_loop(
                feedback or message,
                mode=resolved_mode,
                review_level=review_level,
                capability_scope=capability_scope,
                on_token=on_token,
                on_event=on_event,
            )

        outcome = await self._run_with_review_and_validation(
            message,
            run_once=run_once,
            on_event=on_event,
        )
        return self._finish_loop_outcome(
            outcome,
            message,
            resolved_mode,
            review_level,
            runtime_mode=resolved_mode,
            capability_scope=capability_scope,
        )

    async def resume_review(
        self,
        review_id: str,
        *,
        dag: DAG | None = None,
        approved: bool = True,
        review_level: ReviewLevel | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RuntimeResponse | None:
        state = self.session.get_review_continuation(review_id)
        if state is None:
            return None
        if state.kind == "capability_review":
            response_tokens = _response_token_stream(on_token, on_event)
            initial_outcome = await self.tool_agent.resume_review(
                state,
                approved=approved,
                on_token=response_tokens,
                on_event=on_event,
            )
            if initial_outcome is None:
                return None
            self.session.discard_review_continuation(review_id)

            async def run_once(feedback: str | None) -> LoopOutcome:
                if feedback is None:
                    raise RuntimeError("Tool review validation retry requires feedback.")
                return await self.tool_agent.run(
                    feedback,
                    review_level=state.review_level,
                    capability_scope=state.capability_scope,
                    on_token=response_tokens,
                    on_event=on_event,
                )

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
                extra_invocations=state.invocations,
                task_id=state.task_id,
                capability_scope=state.capability_scope,
            )

        if approved and dag is None:
            return None
        task_id = state.task_id
        record = self.tasks[task_id]
        response_tokens = _response_token_stream(on_token, on_event)
        initial_outcome = await self.dag_agent.resume_review(
            state,
            record=record,
            dag=dag,
            approved=approved,
            review_level=review_level,
            on_token=response_tokens,
            on_event=on_event,
            on_dag=_dag_event_emitter(on_event),
        )
        if initial_outcome is None:
            return None
        self.session.discard_review_continuations_for_task(task_id)
        if initial_outcome.status == "awaiting_review":
            return self._finish_loop_outcome(
                initial_outcome,
                record.user_request,
                "dag",
                review_level or state.review_level,
                task_id=task_id,
                runtime_mode=record.runtime_mode,
                capability_scope=record.capability_scope,
            )

        async def run_once(feedback: str | None) -> LoopOutcome:
            if feedback is None:
                raise RuntimeError("DAG review validation retry requires feedback.")
            return await self.dag_agent.run(
                feedback,
                task_id=record.task_id,
                review_level=review_level or state.review_level,
                runtime_mode=record.runtime_mode,
                capability_scope=record.capability_scope,
                on_token=response_tokens,
                on_event=on_event,
                on_dag=_dag_event_emitter(on_event),
            )

        outcome = await self._run_with_review_and_validation(
            record.user_request,
            run_once=run_once,
            initial_loop_outcome=initial_outcome,
            on_event=on_event,
        )
        return self._finish_loop_outcome(
            outcome,
            record.user_request,
            "dag",
            review_level or state.review_level,
            task_id=task_id,
            runtime_mode=record.runtime_mode,
            capability_scope=record.capability_scope,
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
        mode: _LoopExecutionMode,
        review_level: ReviewLevel,
        on_token: TokenHandler | None,
        on_event: LoopEventHandler | None,
        workspace_root: str | Path = ".dagent-runs",
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        graph_input: Any = None,
    ) -> LoopOutcome:
        """Dispatch to the appropriate loop and return a unified LoopOutcome."""
        if mode == "dag":
            if not isinstance(request, str):
                raise TypeError("DAG message execution requires a string request.")
            response_tokens = _response_token_stream(on_token, on_event)
            return await self.dag_agent.run(
                request,
                task_id=None,
                review_level=review_level,
                runtime_mode=str(mode),
                capability_scope=capability_scope,
                on_token=response_tokens,
                on_event=on_event,
                on_dag=_dag_event_emitter(on_event),
            )
        elif mode == "tool":
            if not isinstance(request, str):
                raise TypeError("Tool execution requires a string request.")
            response_tokens = _response_token_stream(on_token, on_event)
            return await self.tool_agent.run(
                request,
                review_level=review_level,
                capability_scope=capability_scope,
                on_token=response_tokens,
                on_event=on_event,
            )
        elif mode == "dag_spec":
            if not isinstance(request, DAGSpec):
                raise TypeError("DAGSpec execution requires a DAGSpec request.")
            response_tokens = _response_token_stream(on_token, on_event)
            return await self.dag_agent.loop.run_static(
                request,
                graph_input=graph_input,
                workspace_root=workspace_root,
                artifact_uploads=artifact_uploads,
                on_token=response_tokens,
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
        extra_invocations: list[CapabilityInvocation] | None = None,
        task_id: str | None = None,
        runtime_mode: str | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
    ) -> RuntimeResponse:
        invocations = [*(extra_invocations or []), *outcome.invocations]
        if outcome.status == "awaiting_review":
            record = self.session.save_loop_outcome(
                task_id=task_id,
                mode=mode,
                user_request=user_request,
                review_level=review_level,
                loop_outcome=outcome,
                invocations=invocations,
                runtime_mode=runtime_mode,
                capability_scope=capability_scope,
            )
            return _gate_result_for_task(outcome, record.task_id)

        record = self.session.save_loop_outcome(
            task_id=task_id,
            mode=mode,
            user_request=user_request,
            review_level=review_level,
            loop_outcome=outcome,
            invocations=invocations,
            runtime_mode=runtime_mode,
            capability_scope=capability_scope,
        )
        final_answer = outcome.final_answer.strip() or _fallback_final_answer(outcome)
        return RuntimeResponse(
            status=outcome.status,
            final_answer=final_answer,
            dag=outcome.dag,
            trace=record.trace or outcome.trace,
            task_id=record.task_id,
            events=outcome.events,
            pending_review=outcome.pending_review,
        )

    async def run_dag_spec(
        self,
        spec: DAGSpec,
        *,
        input: Any = None,
        workspace_root: str | Path = ".dagent-runs",
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> DAGRun:
        outcome = await self._execute_loop(
            spec,
            mode="dag_spec",
            review_level="fast",
            on_token=on_token,
            on_event=on_event,
            workspace_root=workspace_root,
            artifact_uploads=artifact_uploads,
            graph_input=input,
        )
        record = self.session.save_loop_outcome(
            task_id=outcome.task_id,
            mode="dag",
            user_request=f"Run DAGSpec {spec.id}",
            review_level="fast",
            loop_outcome=outcome,
            runtime_mode="dag_spec",
        )
        trace = record.trace or outcome.trace
        if trace is None:
            raise RuntimeError(f"DAGSpec run '{record.task_id}' completed without a run trace.")
        return DAGRun(
            run_id=record.task_id,
            spec_id=record.spec_id or spec.id,
            workspace_path=record.workspace_path or "",
            dag=record.dag,
            trace=trace,
        )


def _fallback_final_answer(loop_outcome: LoopOutcome) -> str:
    if loop_outcome.execution_context.strip():
        return loop_outcome.execution_context.strip()
    if loop_outcome.status == "completed":
        return "The task completed, but no final answer was produced."
    return "The task did not complete, and no final answer was produced."


def _response_token_stream(
    on_token: TokenHandler | None,
    on_event: LoopEventHandler | None,
) -> TokenHandler | None:
    if on_token is None and on_event is None:
        return None
    return _ResponseTokenSplitter(on_raw=on_token, on_event=on_event)


def _gate_result_for_task(loop_outcome: LoopOutcome, task_id: str) -> RuntimeResponse:
    return RuntimeResponse(
        status="awaiting_review",
        final_answer="",
        dag=loop_outcome.dag,
        trace=loop_outcome.trace,
        task_id=task_id,
        events=loop_outcome.events,
        pending_review=loop_outcome.pending_review,
    )


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
