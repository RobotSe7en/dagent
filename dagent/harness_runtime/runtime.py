"""Top-level harness runtime.

The runtime orchestrates: route -> loop -> review -> validation -> final result.
It does not know how loops execute internally; it only consumes LoopOutcome.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, MutableMapping
import hashlib
import mimetypes
from typing import Any, Literal
from pathlib import Path
from uuid import uuid4

from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.capabilities.sandbox import SandboxExecutionError
from dagent.capabilities.sandbox_context import current_run_execution
from dagent.capabilities.workspace import current_workspace_root
from dagent.harness_runtime.tool_agent import (
    ToolAgent,
    LoopEventHandler,
    TokenHandler,
)
from dagent.capabilities.catalog import CapabilityHandler
from dagent.harness_runtime.capability_executor import CapabilityExecutionContext, CapabilityExecutor
from dagent.harness_runtime.capability_scope import (
    CapabilityScope,
    DEFAULT_CAPABILITY_SCOPE,
    capability_scope_from_state,
    capability_scope_to_state,
)
from dagent.harness_runtime.dag_agent import DAGAgent
from dagent.harness_runtime.dag_builder import validate_dag_input
from dagent.harness_runtime.dag_executor import DAGExecutor
from dagent.harness_runtime.conversation_resources import ConversationResourceStore
from dagent.harness_runtime.execution_budget import (
    ExecutionLimitExceeded,
    reserve_model_turn,
)
from dagent.harness_runtime.artifacts import (
    ArtifactUpload,
    create_run_workspace,
    materialize_workbench_uploads,
)
from dagent.harness_runtime.validator_agent import ValidatorAgent, format_validation_feedback
from dagent.harness_runtime.runtime_session import HarnessRuntimeSession
from dagent.harness_runtime.runtime_events import _dag_event_emitter
from dagent.review import ReviewLevel
from dagent.providers import ChatProvider
from dagent.providers.base import normalize_chat_response
from dagent.result import RunResult
from dagent.schemas import (
    AssistantMessage,
    Attachment,
    ContextUsage,
    ConversationItem,
    ConversationState,
    DAG,
    DAGSpec,
    LoopOutcome,
    CapabilityDefinition,
    RunState,
    UserMessage,
)
from dagent.schemas.run_id import validate_run_id
from dagent.schemas.common import validate_runtime_directory
from dagent.config import DEFAULT_RUNS_DIR, resolve_run_workspace_root


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


LoopRunner = Callable[[UserMessage | None], Awaitable[LoopOutcome]]


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
        runtime_directory: str,
    ) -> None:
        self.provider = provider
        self.tool_agent = tool_agent
        self.dag_agent = dag_agent
        self.validator = validator
        self.enable_validation = enable_validation
        self.max_validation_retries = max_validation_retries
        self.runtime_directory = validate_runtime_directory(runtime_directory)
        self.session = HarnessRuntimeSession()
        self.runs = self.session.runs
        if capability_catalog is None and capability_executor is not None:
            capability_catalog = capability_executor.catalog
        self.capability_catalog = capability_catalog or CapabilityCatalog()
        self.conversation_resources = ConversationResourceStore(
            self.capability_catalog.workspace_root,
            self.runtime_directory,
        )
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
    ) -> tuple[
        bool,
        str | None,
        AssistantMessage | None,
        ContextUsage | None,
    ]:
        passed = True
        feedback: str | None = None
        if not self.enable_validation or not self.validator:
            return passed, feedback, None, None
        if not loop_outcome.execution_context.strip():
            return passed, feedback, None, None
        if on_event:
            on_event({"type": "validating", "message": "Validating result quality..."})
        validation, response, context_usage = await self.validator.validate_with_audit(
            user_request=user_request,
            final_answer=loop_outcome.output_text,
            execution_context=loop_outcome.execution_context,
            workspace_path=loop_outcome.state.workspace_path,
        )
        audit_item = (
            None
            if response is None
            else AssistantMessage(
                run_id=loop_outcome.state.run_id,
                content=response.content,
                reasoning=response.reasoning_content,
                refusal=response.refusal,
                usage=response.usage,
                scope="validator",
                visibility="internal",
            )
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
            return passed, feedback, audit_item, context_usage
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
        return passed, feedback, audit_item, context_usage

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
        audit_items: list[ConversationItem] = []
        run_context_usages: list[ContextUsage] = []
        validation_usages: list[ContextUsage] = []

        for _attempt in range(self.max_validation_retries + 1):
            if loop_outcome is None or feedback is not None:
                feedback_item = (
                    None
                    if feedback is None
                    else UserMessage(
                        run_id=(
                            None
                            if loop_outcome is None
                            else loop_outcome.state.run_id
                        ),
                        content=feedback,
                        scope="validator",
                        visibility="internal",
                    )
                )
                if feedback_item is not None:
                    audit_items.append(feedback_item)
                loop_outcome = await run_once(feedback_item)
                audit_items.extend(loop_outcome.new_items)
                run_context_usages.extend(loop_outcome.state.context_usage)
            elif not audit_items:
                audit_items.extend(loop_outcome.new_items)
                run_context_usages.extend(loop_outcome.state.context_usage)

            if loop_outcome.state.status == "awaiting_review":
                return loop_outcome.model_copy(
                    update={"new_items": tuple(audit_items)}
                )

            passed, validation_feedback, audit_item, context_usage = (
                await self._validate_loop_outcome(
                    loop_outcome,
                    user_request,
                    on_event=on_event,
                )
            )
            if audit_item is not None:
                audit_items.append(audit_item)
            if context_usage is not None:
                validation_usages.append(context_usage)
            if passed:
                state = loop_outcome.state.model_copy(
                    update={
                        "context_usage": [
                            *run_context_usages,
                            *validation_usages,
                        ]
                    }
                )
                return loop_outcome.model_copy(
                    update={
                        "state": state,
                        "new_items": tuple(audit_items),
                    }
                )
            feedback = validation_feedback

        if loop_outcome is None:
            raise RuntimeError("Validation retry loop did not execute.")
        state = loop_outcome.state.model_copy(
            update={
                "context_usage": [
                    *run_context_usages,
                    *validation_usages,
                ]
            }
        )
        return loop_outcome.model_copy(
            update={
                "state": state,
                "new_items": tuple(audit_items),
            }
        )

    async def handle_input(
        self,
        input: str,
        *,
        conversation: ConversationState | None = None,
        mode: RuntimeMode = "auto",
        review_level: ReviewLevel = "fast",
        dynamic_adjust: bool = True,
        workspace_root: str | Path = DEFAULT_RUNS_DIR,
        workspace_path: str | Path | None = None,
        run_id: str | None = None,
        input_uploads: list[ArtifactUpload] | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        if run_id is not None:
            validate_run_id(run_id)
        if not isinstance(input, str):
            raise TypeError("input must be a string.")
        user_request = input
        resolved_mode = mode
        route_item: AssistantMessage | None = None
        route_usage: ContextUsage | None = None
        if mode == "auto":
            resolved_mode, route_item, route_usage = await self._route(user_request)
        resolved_run_id = run_id or _new_run_id_for_mode(resolved_mode)
        if route_item is not None:
            route_item = route_item.model_copy(update={"run_id": resolved_run_id})
        resolved_workspace_path = self._workspace_path_for_run(
            workspace_root,
            resolved_run_id,
            workspace_path=workspace_path,
        )
        base_conversation = self.conversation_resources.materialize(
            conversation or ConversationState(),
            workspace_path=resolved_workspace_path,
        )
        materialized_uploads = materialize_workbench_uploads(
            input_uploads or [],
            workspace_path=resolved_workspace_path,
        )
        input_item = UserMessage(
            run_id=resolved_run_id,
            content=user_request,
            attachments=_workbench_attachments(
                resolved_workspace_path,
                materialized_uploads,
            ),
        )
        run_conversation = _append_conversation_item(
            base_conversation,
            input_item,
        )
        _emit_run_started(on_event, run_id=resolved_run_id, kind=_state_kind_for_mode(resolved_mode))

        async def run_once(feedback: UserMessage | None) -> LoopOutcome:
            nonlocal run_conversation
            if feedback is not None:
                run_conversation = _append_conversation_item(
                    run_conversation,
                    feedback,
                )
            next_outcome = await self._execute_loop(
                user_request,
                run_id=resolved_run_id,
                mode=resolved_mode,
                review_level=review_level,
                dynamic_adjust=dynamic_adjust,
                workspace_path=resolved_workspace_path,
                capability_scope=capability_scope,
                conversation=run_conversation,
                on_token=on_token,
                on_event=on_event,
            )
            run_conversation = (
                (
                    next_outcome.state.model_thread
                    if resolved_mode == "tool"
                    else next_outcome.state.conversation
                )
                or run_conversation
            )
            return next_outcome

        outcome = await self._run_with_review_and_validation(
            user_request,
            run_once=run_once,
            on_event=on_event,
        )
        if route_item is not None or route_usage is not None:
            state = outcome.state.model_copy(
                update={
                    "context_usage": [
                        *([] if route_usage is None else [route_usage]),
                        *outcome.state.context_usage,
                    ]
                }
            )
            outcome = outcome.model_copy(
                update={
                    "state": state,
                    "new_items": tuple(
                        [
                            *([] if route_item is None else [route_item]),
                            *outcome.new_items,
                        ]
                    ),
                }
            )
        outcome = outcome.model_copy(
            update={"new_items": (input_item, *outcome.new_items)}
        )
        return self._finish_loop_outcome(
            outcome,
            user_request,
            resolved_mode,
            review_level,
            runtime_mode=resolved_mode,
            capability_scope=capability_scope,
        )

    async def resume_review(
        self,
        review_id: str,
        *,
        run_state: RunState | None = None,
        dag: DAG | None = None,
        approved: bool = True,
        review_level: ReviewLevel | None = None,
        feedback: str | None = None,
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
        _emit_run_started(on_event, run_id=state.run_id, kind=state.kind)
        if state.kind == "static_dag" and pending_review.kind == "capability_review":
            if state.dag_spec is None:
                raise ValueError("Static DAG review state requires its DAGSpec.")
            outcome = await self.dag_agent.loop.resume_static_review(
                state,
                approved=approved,
                feedback=feedback,
                on_token=on_token,
                on_event=on_event,
                on_dag=_dag_event_emitter(on_event),
            )
            if outcome is None:
                return None
            self.session.discard_review(review_id)
            return self._finish_static_dag_outcome(outcome, spec=state.dag_spec)
        if pending_review.kind == "capability_review":
            self.tool_agent.conversation = (
                state.model_thread
                or state.conversation
                or ConversationState()
            )
            self.tool_agent.trace = state.trace
            with self.capability_executor.workspace_context(state.workspace_path):
                initial_outcome = await self.tool_agent.resume_review(
                    state,
                    approved=approved,
                    feedback=feedback,
                    on_token=on_token,
                    on_event=on_event,
                )
            if initial_outcome is None:
                return None
            self.session.discard_review(review_id)
            retry_outcome = initial_outcome

            async def run_once(feedback: UserMessage | None) -> LoopOutcome:
                nonlocal retry_outcome
                if feedback is None:
                    raise RuntimeError("Tool review validation retry requires feedback.")
                retry_conversation = _append_conversation_item(
                    retry_outcome.state.model_thread
                    or retry_outcome.state.conversation
                    or ConversationState(),
                    feedback,
                )
                previous_trace = retry_outcome.state.trace
                with self.capability_executor.workspace_context(state.workspace_path):
                    next_outcome = await self.tool_agent.run_conversation(
                        retry_conversation,
                        run_id=state.run_id,
                        review_level=state.review_level,
                        capability_context=CapabilityExecutionContext(
                            task_id=state.run_id,
                            workspace_path=state.workspace_path,
                            skills=capability_scope.skills,
                        ),
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
            )

        # DAG review resume does not flow through _execute_loop, so enforce the
        # sandbox DAG restriction here too (defense in depth; normally a sandbox
        # DAG run can't reach awaiting_review because the loop guard rejects it).
        if current_run_execution() == "sandbox":
            raise SandboxExecutionError(
                "Sandbox execution is not yet supported for DAG-based runs; "
                "use a tool agent or execution='local'."
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
            feedback=feedback,
            dag_executor=self._dag_executor_for_run(state.workspace_path),
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
            )

        retry_outcome = initial_outcome

        async def run_once(feedback: UserMessage | None) -> LoopOutcome:
            nonlocal retry_outcome
            if feedback is None:
                raise RuntimeError("DAG review validation retry requires feedback.")
            retry_conversation = _append_conversation_item(
                retry_outcome.state.conversation or ConversationState(),
                feedback,
            )
            retry_outcome = await self.dag_agent.run_conversation(
                retry_conversation,
                task_id=state.run_id,
                review_level=review_level or state.review_level,
                runtime_mode=state.runtime_mode,
                dynamic_adjust=state.dynamic_adjust,
                workspace_path=state.workspace_path,
                dag_executor=self._dag_executor_for_run(state.workspace_path),
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
        )

    # ==================================================================
    # Route
    # ==================================================================

    async def _route(
        self,
        message: str,
    ) -> tuple[Literal["dag", "tool"], AssistantMessage | None, ContextUsage]:
        """Lightweight LLM classifier."""
        prepared = await self.tool_agent.context_assembler.prepare(
            system_message={"role": "system", "content": _ROUTE_SYSTEM_PROMPT},
            conversation=ConversationState(
                items=(
                    UserMessage(
                        content=message,
                        scope="router",
                        visibility="internal",
                    ),
                )
            ),
            policy=self.tool_agent.context_policy,
        )
        try:
            reserve_model_turn()
            response = normalize_chat_response(
                await self.provider.chat(prepared.messages)
            )
            decision = response.content.strip().lower()
            audit_item = AssistantMessage(
                content=response.content,
                reasoning=response.reasoning_content,
                refusal=response.refusal,
                usage=response.usage,
                scope="router",
                visibility="internal",
            )
            if decision in {"dag", "tool"}:
                return decision, audit_item, prepared.usage  # type: ignore[return-value]
            return "tool", audit_item, prepared.usage
        except ExecutionLimitExceeded:
            raise
        except Exception:
            return "tool", None, prepared.usage

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
        dynamic_adjust: bool = True,
        workspace_root: str | Path = DEFAULT_RUNS_DIR,
        workspace_path: str | Path | None = None,
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        graph_input: Any = None,
        conversation: ConversationState | None = None,
    ) -> LoopOutcome:
        """Dispatch to the appropriate loop and return a unified LoopOutcome."""
        if mode != "tool" and current_run_execution() == "sandbox":
            # DAG execution builds its own per-node workspace context, which
            # would shadow the sandbox run workspace bind-mounted into the
            # container. Until that is unified, only tool-agent runs are
            # supported under the sandbox.
            raise SandboxExecutionError(
                "Sandbox execution is not yet supported for DAG-based runs "
                f"(mode={mode!r}); use a tool agent or execution='local'."
            )
        if mode == "dag":
            if not isinstance(request, str):
                raise TypeError("DAG message execution requires a string request.")
            if conversation is not None:
                return await self.dag_agent.run_conversation(
                    conversation,
                    task_id=run_id,
                    review_level=review_level,
                    runtime_mode=str(mode),
                    dynamic_adjust=dynamic_adjust,
                    workspace_path=workspace_path,
                    dag_executor=self._dag_executor_for_run(workspace_path),
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
                dynamic_adjust=dynamic_adjust,
                workspace_path=workspace_path,
                dag_executor=self._dag_executor_for_run(workspace_path),
                capability_scope=capability_scope,
                on_token=on_token,
                on_event=on_event,
                on_dag=_dag_event_emitter(on_event),
            )
        elif mode == "tool":
            if not isinstance(request, str):
                raise TypeError("Tool execution requires a string request.")
            with self.capability_executor.workspace_context(workspace_path):
                if conversation is not None:
                    return await self.tool_agent.run_conversation(
                        conversation,
                        run_id=run_id,
                        review_level=review_level,
                        capability_context=CapabilityExecutionContext(
                            task_id=run_id,
                            workspace_path=workspace_path,
                            skills=capability_scope.skills,
                        ),
                        capability_scope=capability_scope,
                        on_token=on_token,
                        on_event=on_event,
                    )
                return await self.tool_agent.run(
                    request,
                    run_id=run_id,
                    review_level=review_level,
                    capability_context=CapabilityExecutionContext(
                        task_id=run_id,
                        workspace_path=workspace_path,
                        skills=capability_scope.skills,
                    ),
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
                review_level=review_level,
                capability_scope=capability_scope,
                workspace_root=workspace_root,
                workspace_path=workspace_path,
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
    ) -> RunResult:
        final_answer = (
            ""
            if outcome.state.status == "awaiting_review"
            else outcome.output_text.strip() or _fallback_output_text(outcome)
        )
        conversation = outcome.state.conversation
        new_items = list(outcome.new_items)
        if (
            mode == "dag"
            and outcome.state.status != "awaiting_review"
            and conversation is not None
            and final_answer
        ):
            answer = AssistantMessage(
                run_id=outcome.state.run_id,
                content=final_answer,
            )
            conversation = _append_conversation_item(conversation, answer)
            new_items.append(answer)
        if conversation is not None and outcome.state.workspace_path:
            self.conversation_resources.persist(
                conversation,
                workspace_path=outcome.state.workspace_path,
            )
        update: dict[str, Any] = {
            "kind": _state_kind_for_mode(mode),
            "status": outcome.state.status,
            "conversation": conversation,
            "user_request": user_request,
            "review_level": review_level,
            "runtime_mode": runtime_mode or mode,
            "execution": current_run_execution(),
            "capability_scope": capability_scope_to_state(capability_scope),
            "pending_review": outcome.state.pending_review,
            "pending_invocation": outcome.state.pending_invocation,
        }
        # Low-level callers may invoke loops under a sandbox context without
        # providing a run workspace. Preserve the mounted capability workspace
        # rather than leaving the state blank.
        if current_run_execution() == "sandbox" and not outcome.state.workspace_path:
            # In sandbox scope the workspace contextvar is always set by the
            # runner; the default is a never-used fallback to satisfy the API.
            update["workspace_path"] = str(current_workspace_root("."))
        state = outcome.state.model_copy(update=update)
        state = self.session.save_run_state(state)
        return RunResult(
            state=state,
            output_text=final_answer,
            new_items=tuple(new_items),
        )

    async def run_dag_spec(
        self,
        spec: DAGSpec,
        *,
        graph_input: Any = None,
        workspace_root: str | Path = DEFAULT_RUNS_DIR,
        workspace_path: str | Path | None = None,
        run_id: str | None = None,
        review_level: ReviewLevel = "fast",
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        if run_id is not None:
            validate_run_id(run_id)
        validate_dag_input(spec, graph_input)
        resolved_run_id = run_id or _new_run_id_for_mode("dag_spec")
        resolved_workspace_root = self._resolve_run_workspace_root(workspace_root)
        _emit_run_started(on_event, run_id=resolved_run_id, kind="static_dag")
        outcome = await self._execute_loop(
            spec,
            run_id=resolved_run_id,
            mode="dag_spec",
            review_level=review_level,
            on_token=on_token,
            on_event=on_event,
            workspace_root=resolved_workspace_root,
            workspace_path=workspace_path,
            artifact_uploads=artifact_uploads,
            graph_input=graph_input,
            capability_scope=capability_scope,
        )
        return self._finish_static_dag_outcome(outcome, spec=spec)

    def _finish_static_dag_outcome(
        self,
        outcome: LoopOutcome,
        *,
        spec: DAGSpec,
    ) -> RunResult:
        state = self.session.save_run_state(
            outcome.state.model_copy(update={"execution": current_run_execution()})
        )
        if state.trace is None:
            raise RuntimeError(f"DAGSpec run '{state.run_id}' completed without a run trace.")
        return RunResult(
            state=state,
            output_text=outcome.output_text.strip() or _fallback_output_text(outcome),
            output_value=(
                state.trace.root.value
                if state.status == "completed" and spec.output is not None
                else None
            ),
            new_items=outcome.new_items,
        )

    def _resolve_run_workspace_root(self, workspace_root: str | Path) -> Path:
        return resolve_run_workspace_root(self.capability_catalog.workspace_root, workspace_root)

    def _workspace_path_for_run(
        self,
        workspace_root: str | Path,
        run_id: str,
        *,
        workspace_path: str | Path | None = None,
    ) -> Path:
        if workspace_path is not None:
            path = Path(workspace_path).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
            return path
        return create_run_workspace(self._resolve_run_workspace_root(workspace_root), run_id=run_id)

    def _dag_executor_for_run(self, workspace_path: str | Path | None) -> DAGExecutor:
        base = self.dag_agent.loop.dag_executor
        capability_workspace_root = (
            Path(workspace_path).resolve()
            if workspace_path is not None
            else base.capability_workspace_root or base.capability_executor.workspace_root
        )
        return DAGExecutor(
            capability_executor=base.capability_executor,
            workspace_path=workspace_path,
            capability_workspace_root=capability_workspace_root,
            runtime_directory=self.runtime_directory,
            result_storage_policy=base.result_storage_policy,
            extra_system_prompt=base.extra_system_prompt,
        )


def _fallback_output_text(loop_outcome: LoopOutcome) -> str:
    if loop_outcome.execution_context.strip():
        return loop_outcome.execution_context.strip()
    if loop_outcome.state.status == "completed":
        return "The task completed, but no final answer was produced."
    return "The task did not complete, and no final answer was produced."


def _append_conversation_item(
    conversation: ConversationState,
    item: ConversationItem,
) -> ConversationState:
    return conversation.model_copy(
        update={
            "revision": conversation.revision + 1,
            "items": (*conversation.items, item),
        }
    )


def _workbench_attachments(
    workspace_path: Path,
    upload_paths: list[str],
) -> tuple[Attachment, ...]:
    attachments: list[Attachment] = []
    for relative_path in upload_paths:
        path = workspace_path / relative_path
        data = path.read_bytes()
        media_type = mimetypes.guess_type(relative_path)[0] or "application/octet-stream"
        attachments.append(
            Attachment(
                path=Path(relative_path).as_posix(),
                media_type=media_type,
                byte_length=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    return tuple(attachments)


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
