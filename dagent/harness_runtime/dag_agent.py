"""DAG agent and loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from dagent.capabilities.toolsets import CapabilityToolAdapter
from dagent.harness_runtime.capability_scope import CapabilityScope, DEFAULT_CAPABILITY_SCOPE
from dagent.harness_runtime.builder_translator import translate_builder_source
from dagent.harness_runtime.artifacts import (
    ArtifactUpload,
    create_run_workspace,
    init_artifact_states,
    materialize_artifact_uploads,
)
from dagent.harness_runtime.dag_executor import (
    DAGExecutionError,
    DAGExecutor,
    SETTLED_NODE_STATUSES,
)
from dagent.harness_runtime.dag_builder import (
    DAGCreationError,
    DAGValidationError,
    MAX_EXECUTION_CONTEXT_CHARS,
    compile_dag_spec,
    context_excerpt,
    validate_dag,
)
from dagent.harness_runtime.dynamic_planner import (
    DYNAMIC_GRAPH_INPUT_SCHEMA,
    normalize_builder_dag,
    normalize_planner_graph,
    resolve_dag_spec_capabilities,
)
from dagent.harness_runtime.planner_schema import (
    builder_planner_response_format,
    parse_builder_planner_response,
    parse_planner_response,
    planner_response_format,
)
from dagent.review import ReviewLevel, _append_reviewer_feedback, _review_policy
from dagent.harness_runtime.capability_scope import capability_scope_from_state, capability_scope_to_state
from dagent.harness_runtime.runtime_events import ResponseStreamContext, response_token_stream
from dagent.harness_runtime.llm_retry import (
    DEFAULT_LLM_RETRY_POLICY,
    LLMRetryPolicy,
    LLMRetrySleep,
    run_with_llm_retries,
)
from dagent.harness_runtime.execution_budget import (
    ExecutionLimitExceeded,
    reserve_model_turn,
)
from dagent.profiles import AgentProfile
from dagent.providers import ChatProvider, ChatResponse, StructuredOutputFormat
from dagent.providers.base import normalize_chat_response
from dagent.schemas import (
    AssistantMessage,
    Artifact,
    ArtifactState,
    DAG,
    DAGEdge,
    DAGNode,
    DAGSpec,
    LoopOutcome,
    LoopStatus,
    PendingReview,
    PlannerFrontend,
    PlannerSkillSnapshot,
    CapabilityDefinition,
    CapabilityNodePayload,
    ContextPolicy,
    ContextSummary,
    ContextUsage,
    ContextWindowExceeded,
    ConversationItem,
    ConversationState,
    LoopNodePayload,
    MapNodePayload,
    RunTrace,
    RunTraceError,
    RunTraceNode,
    RunTraceStatus,
    RunState,
    ResultStoragePolicy,
    StartNodePayload,
    SubgraphNodePayload,
    ToolCallItem,
    ToolResultMessage,
    UserMessage,
    iter_dag_invocations,
)
from dagent.config import DEFAULT_RUNS_DIR, resolve_run_workspace_root
from dagent.schemas.node import NodeStatus
from dagent.schemas.value import iter_artifact_exprs
from dagent.state import PromptBuilder, PromptRequest
from dagent.harness_runtime.context import ContextAssembler, user_content_for_model
from dagent.schemas.conversation import inline_content, stored_content_text


MAX_NODE_RESULT_CONTEXT_CHARS = 4000
MAX_CAPABILITY_ARGS_CONTEXT_CHARS = 2000


@dataclass(frozen=True)
class _PlannerProposal:
    spec: DAGSpec
    rerun_nodes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ReplanChanges:
    node_ids: frozenset[str]
    artifact_ids: frozenset[str]

    @property
    def has_changes(self) -> bool:
        return bool(self.node_ids or self.artifact_ids)


class DAGAgent:
    """Runtime-facing DAG agent facade."""

    def __init__(
        self,
        *,
        loop: "DAGAgentLoop",
        profile: AgentProfile,
        prompt_builder: PromptBuilder | None = None,
        extra_system_prompt: str | None = None,
        context_policy: ContextPolicy | None = None,
        result_storage_policy: ResultStoragePolicy | None = None,
        context_assembler: ContextAssembler | None = None,
    ) -> None:
        self.loop = loop
        self.profile = profile
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.extra_system_prompt = extra_system_prompt
        self.context_policy = context_policy or ContextPolicy()
        self.result_storage_policy = result_storage_policy or ResultStoragePolicy()
        self.context_assembler = context_assembler or ContextAssembler(
            context_window_tokens=getattr(loop.provider, "context_window_tokens", 32768),
            output_reserve_tokens=getattr(loop.provider, "output_reserve_tokens", 4096),
        )
        self.loop.context_policy = self.context_policy
        self.loop.context_assembler = self.context_assembler
        self.loop.result_storage_policy = self.result_storage_policy
        self.tools = self.loop.available_capabilities()
        self.system_message = self._build_system_message(DEFAULT_CAPABILITY_SCOPE)
        self.thread = ConversationState()

    def _build_system_message(
        self,
        capability_scope: CapabilityScope,
        *,
        workspace_path: str | Path | None = None,
    ) -> dict[str, str]:
        tools = self.loop.available_capabilities(capability_scope.capability_ids)
        context_sections = [
            _planner_response_schema_context(
                _planner_response_format_for_frontend(self.loop.planner_frontend)
            )
        ]
        if self.loop.planner_frontend == "sdk_builder":
            planner_skill = self.loop.planner_skill
            if planner_skill is None:
                raise RuntimeError("sdk_builder planner frontend requires a frozen planner skill.")
            context_sections.append(
                _builder_frontend_context()
                + "\n\n## Mandatory Planner Skill "
                f"(version {planner_skill.version}, sha256 {planner_skill.sha256})\n"
                + planner_skill.content
            )
        context_sections.append(_planner_capability_context(tools))
        return self.prompt_builder.build_system_message(
            PromptRequest(
                profile=self.profile,
                task_content="",
                context="\n\n".join(context_sections),
                extra_system_prompt=self.extra_system_prompt,
                workspace_path=workspace_path,
            )
        )

    def _provider_messages(
        self,
        messages: list[dict[str, Any]],
        capability_scope: CapabilityScope,
        *,
        workspace_path: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        system_message = self._build_system_message(
            capability_scope,
            workspace_path=workspace_path,
        )
        return [dict(system_message), *[dict(message) for message in messages]]

    async def run(
        self,
        request: str,
        *,
        task_id: str | None = None,
        review_level: ReviewLevel = "fast",
        runtime_mode: str = "auto",
        dynamic_adjust: bool = True,
        workspace_path: str | Path | None = None,
        dag_executor: DAGExecutor | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        return await self.run_conversation(
            ConversationState(items=(UserMessage(content=request, run_id=task_id),)),
            task_id=task_id,
            review_level=review_level,
            runtime_mode=runtime_mode,
            dynamic_adjust=dynamic_adjust,
            workspace_path=workspace_path,
            dag_executor=dag_executor,
            capability_scope=capability_scope,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )

    async def run_conversation(
        self,
        conversation: ConversationState,
        *,
        task_id: str | None = None,
        review_level: ReviewLevel = "fast",
        runtime_mode: str = "auto",
        dynamic_adjust: bool = True,
        workspace_path: str | Path | None = None,
        dag_executor: DAGExecutor | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        request = _last_user_content(conversation)
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        self.loop.context_usages = []
        self.loop.audit_items = []
        public_items = conversation.items
        prior_conversation = conversation.model_copy(
            update={"items": public_items[:-1]}
        )
        self.thread = _planner_thread_from_conversation(prior_conversation)
        self.loop.latest_context_summary = self.thread.summary
        raw_messages = _openai_messages_from_thread(self.thread)
        provider_messages = self._provider_messages(
            raw_messages,
            capability_scope,
            workspace_path=workspace_path,
        )
        outcome = await self.loop.run_dynamic(
            request,
            task_id=resolved_task_id,
            review_level=review_level,
            capability_scope=capability_scope,
            messages=provider_messages,
            build_user_message=self.build_request_user_message,
            runtime_mode=runtime_mode,
            dynamic_adjust=dynamic_adjust,
            workspace_path=workspace_path,
            dag_executor=dag_executor,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )
        self.thread = _thread_from_openai_messages(
            _strip_system_message(provider_messages),
            conversation_id=self.thread.id,
            revision=self.thread.revision + 1,
            run_id=resolved_task_id,
            summary=self.loop.latest_context_summary,
        )
        public_conversation = _bounded_public_conversation(
            conversation,
            summary=self.loop.latest_context_summary,
            keep_recent_turns=self.context_policy.keep_recent_turns,
        )
        state = outcome.state.model_copy(
            update={
                "conversation": public_conversation,
                "model_thread": self.thread,
            }
        )
        return outcome.model_copy(
            update={
                "state": state,
                "new_items": (
                    *outcome.new_items,
                    *self.loop.audit_items,
                ),
            }
        )

    async def resume_review(
        self,
        state: RunState,
        *,
        dag: DAG | None = None,
        approved: bool = True,
        review_level: ReviewLevel | None = None,
        feedback: str | None = None,
        dag_executor: DAGExecutor | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome | None:
        self.thread = state.model_thread or ConversationState()
        self.loop.context_usages = list(state.context_usage)
        self.loop.audit_items = []
        self.loop.latest_context_summary = (
            None if state.model_thread is None else state.model_thread.summary
        )
        provider_messages = self._provider_messages(
            _openai_messages_from_thread(self.thread),
            capability_scope_from_state(state.capability_scope),
            workspace_path=state.workspace_path,
        )
        outcome = await self.loop.resume_review(
            state,
            dag,
            approved=approved,
            review_level=review_level,
            feedback=feedback,
            messages=provider_messages,
            build_user_message=self.build_request_user_message,
            dag_executor=dag_executor,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )
        if outcome is None:
            return None
        self.thread = _thread_from_openai_messages(
            _strip_system_message(provider_messages),
            conversation_id=self.thread.id,
            revision=self.thread.revision + 1,
            run_id=state.run_id,
            summary=self.loop.latest_context_summary,
        )
        previous_conversation = state.conversation or ConversationState()
        public_conversation = _bounded_public_conversation(
            previous_conversation,
            summary=self.loop.latest_context_summary,
            keep_recent_turns=self.context_policy.keep_recent_turns,
        )
        if (
            outcome.state.status == "awaiting_review"
            and public_conversation.revision == previous_conversation.revision
        ):
            public_conversation = public_conversation.model_copy(
                update={"revision": previous_conversation.revision + 1}
            )
        next_state = outcome.state.model_copy(
            update={
                "model_thread": self.thread,
                "conversation": public_conversation,
            }
        )
        return outcome.model_copy(
            update={
                "state": next_state,
                "new_items": (
                    *outcome.new_items,
                    *self.loop.audit_items,
                ),
            }
        )

    async def execute(
        self,
        record: RunState,
        *,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
        max_cycles: int | None = None,
    ) -> RunTrace | None:
        return await self.loop.execute(
            record,
            messages=self._provider_messages(
                _openai_messages_from_thread(self.thread),
                capability_scope_from_state(record.capability_scope),
                workspace_path=record.workspace_path,
            ),
            build_user_message=self.build_request_user_message,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
            max_cycles=max_cycles,
        )

    def build_request_user_message(self, *, prompt: str, task_id: str) -> dict[str, str]:
        return self.prompt_builder.build_user_message(
            "Task id: {{ task_id }}\n{{ user_request }}",
            {"user_request": prompt, "task_id": task_id},
        )


class DAGAgentLoop:
    """Owns dynamic and static DAG lifecycle orchestration."""

    def __init__(
        self,
        *,
        provider: ChatProvider,
        dag_executor: DAGExecutor,
        tool_adapter: CapabilityToolAdapter,
        enabled_toolsets: Sequence[str] = ("builtin",),
        max_cycles: int = 6,
        planner_frontend: PlannerFrontend = "typed_spec",
        planner_skill: PlannerSkillSnapshot | None = None,
        _llm_retry_policy: LLMRetryPolicy = DEFAULT_LLM_RETRY_POLICY,
        _llm_retry_sleep: LLMRetrySleep = asyncio.sleep,
    ) -> None:
        self.provider = provider
        self.dag_executor = dag_executor
        self.tool_adapter = tool_adapter
        self.enabled_toolsets = tuple(enabled_toolsets)
        self.max_cycles = max_cycles
        if planner_frontend not in {"typed_spec", "sdk_builder"}:
            raise ValueError("Unknown planner frontend.")
        if planner_frontend == "sdk_builder" and planner_skill is None:
            raise ValueError("sdk_builder planner frontend requires a frozen planner skill.")
        if planner_frontend == "typed_spec" and planner_skill is not None:
            raise ValueError("typed_spec planner frontend cannot use a builder planner skill.")
        self.planner_frontend = planner_frontend
        self.planner_skill = planner_skill
        self.llm_retry_policy = _llm_retry_policy
        self.llm_retry_sleep = _llm_retry_sleep
        self.context_policy = ContextPolicy()
        self.context_assembler = ContextAssembler(
            context_window_tokens=getattr(provider, "context_window_tokens", 32768),
            output_reserve_tokens=getattr(provider, "output_reserve_tokens", 4096),
        )
        self.context_usages: list[ContextUsage] = []
        self.latest_context_summary: ContextSummary | None = None
        self.audit_items: list[ConversationItem] = []
        self.result_storage_policy = ResultStoragePolicy()

    def available_capabilities(
        self,
        capability_ids: Sequence[str] | None = None,
    ) -> list[CapabilityDefinition]:
        return self.tool_adapter.capabilities(
            self.enabled_toolsets,
            capability_ids=capability_ids,
        )

    async def _request_dag(
        self,
        *,
        task_id: str | None,
        messages: list[dict[str, Any]],
        user_message: dict[str, str],
        allow_no_change: bool = True,
        model_step: int | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        current_spec: DAGSpec | None = None,
    ) -> _PlannerProposal | str | None:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        messages.append(dict(user_message))
        self.audit_items.append(
            UserMessage(
                run_id=resolved_task_id,
                content=str(user_message.get("content") or ""),
                scope="planner",
                visibility="internal",
            )
        )
        if not messages or messages[0].get("role") != "system":
            raise ValueError("DAG planner model thread requires one system message.")
        planner_thread = _thread_from_openai_messages(
            _strip_system_message(messages),
            conversation_id=resolved_task_id,
            summary=self.latest_context_summary,
        )
        response_format = _planner_response_format_for_frontend(
            self.planner_frontend
        )
        prepared = await self.context_assembler.prepare(
            system_message=messages[0],
            conversation=planner_thread,
            tools=({"response_format": response_format.schema},),
            policy=self.context_policy,
            compact=lambda summary, items, limit: self._compact_history(
                summary,
                items,
                limit,
                run_id=resolved_task_id,
                on_event=on_event,
            ),
        )
        self.context_usages.append(prepared.usage)
        self.latest_context_summary = prepared.conversation.summary
        if on_event is not None and prepared.usage.compaction_method != "none":
            on_event(
                {
                    "type": "context_compacted",
                    "run_id": resolved_task_id,
                    "scope": "planner",
                    "usage": prepared.usage.model_dump(mode="json"),
                }
            )
        messages[:] = [dict(messages[0]), *prepared.messages[1:]]
        response = await _chat_for_dag(
            self.provider,
            prepared.messages,
            run_id=resolved_task_id,
            model_step=model_step,
            on_token=on_token,
            on_event=on_event,
            retry_policy=self.llm_retry_policy,
            retry_sleep=self.llm_retry_sleep,
            response_format=response_format,
        )
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": response.content,
        }
        if response.reasoning_content:
            assistant_message["reasoning_content"] = response.reasoning_content
        if response.usage is not None:
            assistant_message["usage"] = response.usage.model_dump(mode="python")
        messages.append(assistant_message)
        self.audit_items.append(
            AssistantMessage(
                run_id=resolved_task_id,
                content=response.content,
                reasoning=response.reasoning_content,
                refusal=response.refusal,
                usage=response.usage,
                scope="planner",
                visibility="internal",
            )
        )
        if response.refusal:
            raise DAGCreationError(f"DAG planner refused the request: {response.refusal}")
        try:
            planner_response = (
                parse_builder_planner_response(response.content)
                if self.planner_frontend == "sdk_builder"
                else parse_planner_response(response.content)
            )
        except ValueError as exc:
            raise DAGCreationError(str(exc)) from exc
        if planner_response.action == "no_change":
            if not allow_no_change:
                raise DAGCreationError("DAG agent returned no_change for initial planning.")
            return None
        if planner_response.action == "final_answer":
            return str(planner_response.answer or "").strip()
        capabilities = self.tool_adapter.capabilities(
            self.enabled_toolsets,
            capability_ids=capability_scope.capability_ids,
        )
        version = 1 if current_spec is None else current_spec.version + 1
        if self.planner_frontend == "sdk_builder":
            builder_code = getattr(planner_response, "builder_code", None)
            if builder_code is None:
                raise DAGCreationError("propose_plan response is missing builder_code.")
            dag = translate_builder_source(
                builder_code,
                capability_ids=(definition.id for definition in capabilities),
            )
            spec = normalize_builder_dag(
                dag,
                spec_id=resolved_task_id,
                version=version,
                capabilities=capabilities,
                current=current_spec,
                input_schema=DYNAMIC_GRAPH_INPUT_SCHEMA,
            )
        else:
            plan = getattr(planner_response, "plan", None)
            if plan is None:
                raise DAGCreationError("propose_plan response is missing plan.")
            spec = normalize_planner_graph(
                plan,
                spec_id=resolved_task_id,
                version=version,
                capabilities=capabilities,
                current=current_spec,
                input_schema=DYNAMIC_GRAPH_INPUT_SCHEMA,
            )
        return _PlannerProposal(
            spec=spec,
            rerun_nodes=tuple(planner_response.rerun_nodes),
        )

    async def _compact_history(
        self,
        previous: ContextSummary | None,
        items: tuple[ConversationItem, ...],
        max_tokens: int,
        *,
        run_id: str,
        on_event: Callable[[dict[str, Any]], None] | None,
    ) -> ContextSummary:
        if on_event is not None:
            on_event(
                {
                    "type": "context_compaction_started",
                    "run_id": run_id,
                    "item_count": len(items),
                    "scope": "planner",
                }
            )
        source, source_truncated = self.context_assembler.truncate_text(
            _compaction_source(previous, items),
            max_tokens=max(
                1,
                int(
                    (
                        self.context_assembler.context_window_tokens
                        - self.context_assembler.output_reserve_tokens
                    )
                    * 0.7
                ),
            ),
        )
        system_message = {
            "role": "system",
            "content": (
                "Summarize earlier planner conversation data for later task "
                "continuation. Preserve user requirements, accepted plans, execution "
                "observations, unresolved failures, and constraints. Do not include "
                "hidden reasoning. Return only the summary."
            ),
        }
        prepared = await self.context_assembler.prepare(
            system_message=system_message,
            conversation=ConversationState(
                items=(
                    UserMessage(
                        content=source,
                        scope="compactor",
                        visibility="internal",
                    ),
                )
            ),
            policy=self.context_policy,
        )
        reserve_model_turn()
        response = normalize_chat_response(
            await self.provider.chat(prepared.messages)
        )
        raw_content = response.content.strip()
        if not raw_content:
            raise ValueError("Planner context compactor returned an empty summary.")
        content, output_truncated = self.context_assembler.truncate_text(
            raw_content,
            max_tokens=max_tokens,
        )
        summary = ContextSummary(
            content=content,
            source_item_count=(previous.source_item_count if previous else 0) + len(items),
            method="model",
            source_truncated=source_truncated,
            output_truncated=output_truncated,
            reasoning=response.reasoning_content,
            usage=response.usage,
            context_usage=prepared.usage,
        )
        return summary

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_dynamic(
        self,
        request: str,
        *,
        task_id: str | None = None,
        review_level: ReviewLevel = "fast",
        messages: list[dict[str, Any]],
        build_user_message: Callable[..., dict[str, str]],
        runtime_mode: str = "auto",
        dynamic_adjust: bool = True,
        workspace_path: str | Path | None = None,
        dag_executor: DAGExecutor | None = None,
        capability_scope: CapabilityScope = DEFAULT_CAPABILITY_SCOPE,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        resolved_task_id = task_id or f"task_{uuid4().hex}"
        record = RunState(
            schema_version=3,
            run_id=resolved_task_id,
            kind="dynamic_dag",
            status="completed",
            user_request=request,
            dag=_seed_dag(resolved_task_id),
            review_level=review_level,
            runtime_mode=runtime_mode,  # type: ignore[arg-type]
            dynamic_adjust=dynamic_adjust,
            planner_frontend=self.planner_frontend,
            workspace_path=None if workspace_path is None else str(workspace_path),
            capability_scope=capability_scope_to_state(capability_scope),
        )
        result = await self.execute(
            record,
            messages=messages,
            build_user_message=build_user_message,
            entry_observation=request,
            dag_executor=dag_executor,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )
        record.context_usage = list(self.context_usages)
        return self._finalize_run(record, result)

    async def run_static(
        self,
        spec: DAGSpec,
        *,
        run_id: str | None = None,
        graph_input: Any = None,
        workspace_root: str | Path = DEFAULT_RUNS_DIR,
        workspace_path: str | Path | None = None,
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        run_id = run_id or f"dag_run_{uuid4().hex}"
        dag = compile_dag_spec(
            spec,
            task_id=run_id,
            capabilities=self.tool_adapter.capabilities(self.enabled_toolsets),
        )
        dag = self.prepare_for_review(dag)
        workspace_parent_root = (
            self.dag_executor.capability_workspace_root
            or self.dag_executor.capability_executor.workspace_root
        )
        if workspace_path is None:
            workspace = create_run_workspace(
                resolve_run_workspace_root(workspace_parent_root, workspace_root),
                run_id=run_id,
            )
        else:
            workspace = Path(workspace_path).expanduser().resolve()
            workspace.mkdir(parents=True, exist_ok=True)
        capability_workspace_root = workspace
        artifact_states = init_artifact_states(spec.artifacts)
        materialized_artifact_ids = materialize_artifact_uploads(
            artifact_uploads or {},
            artifacts=spec.artifacts,
            workspace_path=workspace,
        )
        for artifact_id in materialized_artifact_ids:
            artifact = spec.artifacts[artifact_id]
            artifact_states[artifact_id] = ArtifactState(
                id=artifact.id,
                paths=list(artifact.paths),
                status="created",
            )
        dag.status = "approved"
        record = RunState(
            run_id=run_id,
            kind="static_dag",
            status="completed",
            user_request=f"Run DAGSpec {spec.id}",
            dag=dag,
            review_level="fast",
            runtime_mode="dag_spec",
            spec_id=spec.id,
            workspace_path=str(workspace),
            capability_scope=capability_scope_to_state(DEFAULT_CAPABILITY_SCOPE),
        )
        _emit_dag(on_dag, dag)

        dag_executor = DAGExecutor(
            capability_executor=self.dag_executor.capability_executor,
            workspace_path=workspace,
            capability_workspace_root=capability_workspace_root,
            runtime_directory=self.dag_executor.runtime_directory,
            result_storage_policy=self.result_storage_policy,
            extra_system_prompt=self.dag_executor.extra_system_prompt,
            artifacts=spec.artifacts,
            artifact_states=artifact_states,
            spec_id=spec.id,
            graph_input=graph_input,
        )
        trace = await self.execute(
            record,
            replan=False,
            dag_executor=dag_executor,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )
        if trace.status == "completed" and _has_required_artifact_failure(
            spec.artifacts,
            trace.artifacts,
        ):
            trace.root.status = "failed"
            record.dag.status = "failed"
        if trace.status == "failed":
            failure_message = (
                trace.root.error.message
                if trace.root.error is not None
                else "DAG execution failed."
            )
            _mark_planned_artifacts_failed(
                dag_executor.artifact_states,
                failure_message,
            )
            trace.artifacts = dict(dag_executor.artifact_states)
        final_response = dag_run_fallback_message(record, trace)
        if spec.output is not None and trace.status == "completed":
            try:
                output_value = dag_executor.resolve_spec_output(spec.output, trace)
            except Exception as exc:
                trace.root.status = "failed"
                record.dag.status = "failed"
                final_response = f"Failed to resolve DAG output: {exc}"
            else:
                trace.root.value = output_value
                final_response = (
                    output_value
                    if isinstance(output_value, str)
                    else json.dumps(output_value, ensure_ascii=False)
                )
                trace.root.output = final_response
        _emit_dag(on_dag, record.dag)
        return _dag_loop_outcome(
            record=record,
            status="completed" if trace.status == "completed" else "failed",
            final_response=final_response,
            dag=record.dag,
            trace=trace,
            spec_id=spec.id,
            workspace_path=str(workspace),
        )

    async def resume_review(
        self,
        state: RunState,
        dag: DAG | None,
        *,
        approved: bool,
        review_level: ReviewLevel | None = None,
        feedback: str | None = None,
        messages: list[dict[str, Any]],
        build_user_message: Callable[..., dict[str, str]],
        dag_executor: DAGExecutor | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome | None:
        pending_review = state.pending_review
        if pending_review is None or pending_review.kind not in {"initial_dag", "dag_replan"}:
            return None
        record = state
        if record.dag is None:
            raise ValueError("DAG review state requires a DAG.")
        task_id = record.run_id
        if (
            record.dag.status == "completed"
            and record.trace is not None
            and record.pending_review is None
        ):
            trace = record.trace
            return _dag_loop_outcome(
                record=record,
                status="completed" if trace.status == "completed" else "failed",
                final_response=_trace_output(trace) or dag_run_fallback_message(record, trace),
                dag=record.dag,
                trace=trace,
            )
        if review_level is not None:
            record.review_level = review_level

        if not approved:
            record.pending_review = None
            observation = _format_dag_observation(
                kind="review_denied",
                task_id=task_id,
                record=record,
                review_message=_append_reviewer_feedback(
                    "Human reviewer denied the proposed DAG change. Continue without applying it.",
                    feedback,
                ),
            )
            outcome = await self._continue_from_observation(
                record,
                observation,
                messages=messages,
                build_user_message=build_user_message,
                dag_executor=dag_executor,
                on_token=on_token,
                on_event=on_event,
                on_dag=on_dag,
            )
            return outcome

        if dag is None:
            raise ValueError("Approved DAG review requires a submitted DAG.")

        proposed_spec = pending_review.proposed_dag_spec
        if proposed_spec is None:
            raise ValueError(
                "Typed DAG review requires pending_review.proposed_dag_spec; "
                "legacy PlanSpec review checkpoints are not supported."
            )
        capabilities = self.available_capabilities(record.capability_scope.capability_ids)
        reviewed_spec = _spec_from_review_projection(proposed_spec, dag)
        reviewed_spec = resolve_dag_spec_capabilities(reviewed_spec, capabilities)
        prepared = compile_dag_spec(
            reviewed_spec,
            task_id=task_id,
            capabilities=capabilities,
        )
        prepared.dag_id = pending_review.proposed_dag.dag_id
        prepared.version = pending_review.proposed_dag.version
        prepared = self.prepare_for_review(prepared, record.capability_scope.capability_ids)

        rerun_nodes = set(pending_review.rerun_nodes)
        completed = set(_completed_node_traces(record))
        unknown_reruns = rerun_nodes - completed
        if unknown_reruns:
            raise DAGValidationError(
                "rerun_nodes must reference completed nodes: "
                f"{', '.join(sorted(unknown_reruns))}."
            )
        changes = _replan_changes(
            current_dag=record.dag,
            current_spec=record.dag_spec,
            proposed_dag=prepared,
            proposed_spec=reviewed_spec,
            rerun_nodes=rerun_nodes,
        )
        _invalidate_changed_results(record, prepared, set(changes.node_ids))
        record.dag = prepared
        record.dag_spec = reviewed_spec
        record.dag.status = "approved"
        record.dag_boundary_approved_version = record.dag.version
        record.pending_review = None
        _emit_dag(on_dag, record.dag)

        active_executor = dag_executor or self.dag_executor
        active_executor.configure_spec(
            reviewed_spec,
            graph_input={"request": record.user_request},
            artifact_states={} if record.trace is None else record.trace.artifacts,
        )

        result = await self.execute(
            record,
            dag_executor=active_executor,
            messages=messages,
            build_user_message=build_user_message,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )
        return self._finalize_run(record, result)

    # ------------------------------------------------------------------
    # Unified execution loop
    # ------------------------------------------------------------------

    async def execute(
        self,
        record: RunState,
        *,
        messages: list[dict[str, Any]] | None = None,
        build_user_message: Callable[..., dict[str, str]] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
        max_cycles: int | None = None,
        replan: bool = True,
        dag_executor: DAGExecutor | None = None,
        entry_observation: str | None = None,
    ) -> RunTrace | None:
        if record.pending_review is not None:
            raise DAGExecutionError("DAG is awaiting review and is not approved.")
        if (
            record.kind == "dynamic_dag"
            and record.planner_frontend != self.planner_frontend
        ):
            raise DAGExecutionError(
                "Run state planner frontend does not match the active DAG runtime."
            )
        if entry_observation is not None and not replan:
            raise TypeError("entry_observation requires replan=True.")
        self._validate_dag_tools(record.dag, record.capability_scope.capability_ids)
        if replan and (messages is None or build_user_message is None):
            raise TypeError("Dynamic DAG execution requires messages and build_user_message.")

        cycle = 0
        failed_node_id: str | None = None
        cycle_limit = self.max_cycles if max_cycles is None else max_cycles
        pending_observation: str | None = entry_observation
        trace = record.trace
        active_executor = dag_executor or self.dag_executor
        if record.dag_spec is not None:
            active_executor.configure_spec(
                record.dag_spec,
                graph_input={"request": record.user_request},
                artifact_states={} if trace is None else trace.artifacts,
            )

        while cycle < cycle_limit:
            cycle += 1

            if pending_observation is None:
                # Execute next ready layer
                layer_failed = False
                before_count = len(trace.root.children) if trace is not None else 0
                try:
                    previous_node_ids = set(_node_traces_by_id(trace)) if trace is not None else set()
                    layer = await active_executor.execute_next_ready_layer(
                        record.dag,
                        initial_trace=trace,
                        approve_node_boundaries=(
                            record.dag_boundary_approved_version is not None
                            and record.dag_boundary_approved_version == record.dag.version
                        ),
                        skills=record.capability_scope.skills,
                        on_token=on_token,
                        on_event=on_event,
                    )
                except ExecutionLimitExceeded:
                    raise
                except Exception as exc:
                    layer_failed = True
                    trace = self._absorb_partial_results(
                        record,
                        trace,
                        on_dag,
                        dag_executor=active_executor,
                        error_message=str(exc),
                        error_code=type(exc).__name__,
                    )
                    failed_node_id = _latest_failed_node_id_from_trace(
                        trace,
                        valid_node_ids=_dag_node_ids(record.dag),
                    )
                    layer_error = str(exc)

                if not layer_failed:
                    trace = layer
                    new_node_ids = set(_node_traces_by_id(layer)) - previous_node_ids
                    record.trace = layer
                    if new_node_ids and all(
                        isinstance(_node_by_id(record.dag, nid).payload, StartNodePayload)
                        for nid in new_node_ids
                    ):
                        # Synthetic start nodes are bookkeeping, not an execution/replan turn.
                        cycle -= 1
                        continue
                    layer_error = ""

                if (
                    not layer_failed
                    and layer.status == "completed"
                    and record.dag_spec is not None
                    and _has_required_artifact_failure(
                        record.dag_spec.artifacts,
                        active_executor.artifact_states,
                    )
                ):
                    layer_failed = True
                    layer_error = "One or more required DAG artifacts were not created."
                    layer.root.status = "failed"
                    record.dag.status = "failed"
                    record.trace = layer
                resolved_output: Any = None
                if (
                    not layer_failed
                    and layer.status == "completed"
                    and record.dag_spec is not None
                    and record.dag_spec.output is not None
                ):
                    resolved_output = active_executor.resolve_spec_output(
                        record.dag_spec.output,
                        layer,
                    )
                    layer.root.value = resolved_output
                pending_observation = _format_dag_observation(
                    kind="layer_failed" if layer_failed else ("dag_executed" if layer.status == "completed" else "layer_completed"),
                    task_id=record.run_id,
                    record=record,
                    last_error=layer_error if layer_failed else "",
                    failed_node_id=failed_node_id if layer_failed else None,
                    resolved_output=resolved_output,
                )

            can_replan = replan and (record.dynamic_adjust or not _has_real_nodes(record.dag))
            if not can_replan:
                if layer_failed:
                    break
                if trace is not None and trace.status == "completed":
                    break
                if trace is not None and len(trace.root.children) == before_count:
                    trace.root.status = "failed"
                    record.dag.status = "failed"
                    break
                pending_observation = None
                continue

            # Ask LLM for the next step: continue, replan, or finish.
            try:
                response = await self._request_dag(
                    task_id=record.run_id,
                    user_message=build_user_message(
                        prompt=pending_observation,
                        task_id=record.run_id,
                    ),
                    messages=messages,
                    allow_no_change=_has_real_nodes(record.dag),
                    model_step=cycle,
                    on_token=on_token,
                    on_event=on_event,
                    capability_scope=capability_scope_from_state(record.capability_scope),
                    current_spec=record.dag_spec,
                )
            except (ExecutionLimitExceeded, ContextWindowExceeded):
                raise
            except Exception as exc:
                pending_observation = _format_dag_observation(
                    kind="validation_error",
                    task_id=record.run_id,
                    record=record,
                    validation_error=str(exc),
                )
                continue

            if isinstance(response, str):
                trace = _set_trace_output(record, trace, response)
                break

            if response is None:
                if record.dag.status == "failed":
                    pending_observation = _format_dag_observation(
                        kind="validation_error",
                        task_id=record.run_id,
                        record=record,
                        validation_error=(
                            "no_change is not allowed after a failed DAG layer; "
                            "return propose_plan with a repair or final_answer."
                        ),
                    )
                    continue
                if _dag_completed(record):
                    break
                pending_observation = None
                continue

            # Apply the new DAG
            try:
                self._apply_replan(record, response)
            except Exception as exc:
                pending_observation = _format_dag_observation(
                    kind="validation_error",
                    task_id=record.run_id,
                    record=record,
                    validation_error=str(exc),
                )
                continue
            if record.pending_review is None:
                if record.dag_spec is not None:
                    active_executor.configure_spec(
                        record.dag_spec,
                        graph_input={"request": record.user_request},
                        artifact_states={} if trace is None else trace.artifacts,
                    )
                    failed_node_id = None
                _emit_dag(on_dag, record.dag)
            pending_observation = None

            # Review gate
            if record.pending_review is not None:
                break

        return self._finalize(record, trace, on_dag)

    async def _continue_from_observation(
        self,
        record: RunState,
        observation: str,
        *,
        messages: list[dict[str, Any]],
        build_user_message: Callable[..., dict[str, str]],
        dag_executor: DAGExecutor | None = None,
        on_token: Callable[[str], None] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_dag: Callable[[DAG], None] | None = None,
    ) -> LoopOutcome:
        result = await self.execute(
            record,
            messages=messages,
            build_user_message=build_user_message,
            entry_observation=observation,
            dag_executor=dag_executor,
            on_token=on_token,
            on_event=on_event,
            on_dag=on_dag,
        )
        return self._finalize_run(record, result)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _finalize_run(
        self,
        record: RunState,
        result: RunTrace | None,
    ) -> LoopOutcome:
        if record.pending_review is not None:
            return _dag_loop_outcome(
                record=record,
                status="awaiting_review",
                final_response=record.pending_review.message,
                dag=record.pending_review.proposed_dag,
                trace=result,
                pending_review=record.pending_review,
            )
        message = _trace_output(result) or dag_run_fallback_message(record, result)
        outcome_dag = record.dag if _has_real_nodes(record.dag) else None
        return _dag_loop_outcome(
            record=record,
            status="completed" if result.status == "completed" else "failed",
            final_response=message,
            dag=outcome_dag,
            trace=result,
        )

    def _absorb_partial_results(
        self,
        record: RunState,
        trace: RunTrace | None,
        on_dag: Callable[[DAG], None] | None = None,
        *,
        dag_executor: DAGExecutor | None = None,
        error_message: str | None = None,
        error_code: str = "",
    ) -> RunTrace:
        if trace is None:
            trace = _empty_run_trace(run_id=record.run_id, dag=record.dag)
        current_node_ids = _dag_node_ids(record.dag)
        active_executor = dag_executor or self.dag_executor
        for node_id, node_trace in active_executor.partial_node_traces.items():
            if node_id in current_node_ids:
                trace.root.upsert_child(node_trace)
                _node_by_id(record.dag, node_id).status = _node_status_from_trace(node_trace.status)
        for failed_id in _failed_node_ids_from_trace(trace, valid_node_ids=current_node_ids):
            _node_by_id(record.dag, failed_id).status = "failed"
        record.dag.status = "failed"
        trace.root.status = "failed"
        if error_message:
            trace.root.error = RunTraceError(message=error_message, code=error_code)
        record.trace = trace
        _emit_dag(on_dag, record.dag)
        return trace

    def _apply_replan(
        self,
        record: RunState,
        proposal: _PlannerProposal,
    ) -> None:
        is_initial = not _has_real_nodes(record.dag)
        next_spec = proposal.spec.model_copy(deep=True)
        prepared = compile_dag_spec(
            next_spec,
            task_id=record.run_id,
            capabilities=self.available_capabilities(record.capability_scope.capability_ids),
        )
        prepared.task_id = record.run_id
        prepared.dag_id = record.dag.dag_id
        prepared.version = record.dag.version + 1
        prepared = self.prepare_for_review(prepared, record.capability_scope.capability_ids)

        completed = set(_completed_node_traces(record))
        rerun_nodes = set(proposal.rerun_nodes)
        unknown_reruns = rerun_nodes - completed
        if unknown_reruns:
            raise DAGValidationError(
                "rerun_nodes must reference completed nodes: "
                f"{', '.join(sorted(unknown_reruns))}."
            )
        changes = _replan_changes(
            current_dag=record.dag,
            current_spec=record.dag_spec,
            proposed_dag=prepared,
            proposed_spec=next_spec,
            rerun_nodes=rerun_nodes,
        )
        changed = set(changes.node_ids)
        protected_changes = changed.intersection(completed) - rerun_nodes
        if protected_changes:
            raise DAGValidationError(
                "Completed nodes cannot change without explicit rerun_nodes: "
                f"{', '.join(sorted(protected_changes))}."
            )
        previous_boundary_approved_version = record.dag_boundary_approved_version
        needs_review = (
            changes.has_changes
            and _review_policy(record.review_level).reviews_dag_changes()
        )
        if needs_review:
            prepared.status = "review_required"
        else:
            prepared.status = "approved"

        if needs_review:
            record.pending_review = PendingReview(
                review_id=f"review_{uuid4().hex}",
                kind="initial_dag" if is_initial else "dag_replan",
                message=(
                    "Review proposed DAG before execution."
                    if is_initial
                    else "Review proposed DAG revision from replanning."
                ),
                proposed_dag=prepared,
                proposed_dag_spec=next_spec,
                rerun_nodes=tuple(sorted(rerun_nodes)),
                payload={},
            )
        else:
            _invalidate_changed_results(record, prepared, changed)
            record.dag = prepared
            record.dag_spec = next_spec
            if changes.has_changes:
                record.dag_boundary_approved_version = None
            elif previous_boundary_approved_version == record.dag.version - 1:
                record.dag_boundary_approved_version = record.dag.version
            record.pending_review = None

    def _finalize(
        self,
        record: RunState,
        trace: RunTrace | None,
        on_dag: Callable[[DAG], None] | None,
    ) -> RunTrace | None:
        if record.pending_review is not None and trace is None and record.trace is None:
            record.dag.status = "review_required"
            _emit_dag(on_dag, record.pending_review.proposed_dag)
            return None
        trace = trace or record.trace or _empty_run_trace(run_id=record.run_id, dag=record.dag)
        node_traces = _node_traces_by_id(trace)
        dag_node_ids = _dag_node_ids(record.dag)
        all_nodes_completed = bool(dag_node_ids) and all(
            nid in node_traces and node_traces[nid].status in SETTLED_NODE_STATUSES
            for nid in dag_node_ids
        )
        completed = record.dag.status != "failed" and (
            bool(_trace_output(trace)) or all_nodes_completed
        )
        trace.root.status = "completed" if completed else "failed"
        record.trace = trace

        if record.pending_review is not None:
            record.dag.status = "review_required"
        elif record.dag.status != "aborted":
            record.dag.status = "completed" if completed else "failed"
            _sync_dag_node_statuses(record.dag, node_traces)

        visible_dag = (
            record.pending_review.proposed_dag
            if record.pending_review is not None
            else record.dag
        )
        _emit_dag(on_dag, visible_dag)
        return trace

    def prepare_for_review(
        self,
        dag: DAG,
        capability_ids: Sequence[str] | None = None,
    ) -> DAG:
        prepared = self.dag_executor.normalize(dag)
        validate_dag(prepared)
        self._validate_dag_tools(prepared, capability_ids)
        return prepared

    def _validate_dag_tools(
        self,
        dag: DAG,
        capability_ids: Sequence[str] | None = None,
    ) -> None:
        unknown_tools = sorted({
            invocation.capability_id
            for invocation in iter_dag_invocations(dag.nodes)
            if invocation.capability_id
            and not self._is_enabled_capability(invocation.capability_id, capability_ids)
        })
        if unknown_tools:
            available_capabilities = [
                definition.id
                for definition in self.tool_adapter.capabilities(
                    self.enabled_toolsets,
                    capability_ids=capability_ids,
                )
            ]
            raise DAGValidationError(
                "Unknown capability(s): "
                f"{', '.join(unknown_tools)}. "
                "Available capabilities: "
                f"{', '.join(sorted(available_capabilities))}."
            )

    def _is_enabled_capability(
        self,
        capability_id: str,
        capability_ids: Sequence[str] | None = None,
    ) -> bool:
        try:
            self.tool_adapter.ensure_allowed(
                capability_id,
                enabled_toolsets=self.enabled_toolsets,
                capability_ids=capability_ids,
            )
            return True
        except KeyError:
            return False

# ------------------------------------------------------------------
# LLM communication
# ------------------------------------------------------------------

async def _chat_for_dag(
    provider: ChatProvider,
    messages: list[dict[str, Any]],
    *,
    run_id: str | None = None,
    model_step: int | None = None,
    on_token: Callable[[str], None] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    retry_policy: LLMRetryPolicy = DEFAULT_LLM_RETRY_POLICY,
    retry_sleep: LLMRetrySleep = asyncio.sleep,
    response_format: StructuredOutputFormat | None = None,
) -> ChatResponse:
    async def chat_attempt() -> ChatResponse:
        reserve_model_turn()
        return await provider.chat(messages, response_format=response_format)

    if on_token is None and on_event is None:
        return normalize_chat_response(
            await run_with_llm_retries(
                chat_attempt,
                policy=retry_policy,
                sleep=retry_sleep,
            )
        )

    stream = response_token_stream(
        on_raw=on_token,
        on_event=on_event,
        context=ResponseStreamContext.create(run_id=run_id, model_step=model_step),
    )
    if stream is None:
        return normalize_chat_response(
            await run_with_llm_retries(
                chat_attempt,
                policy=retry_policy,
                sleep=retry_sleep,
            )
        )

    content = ""
    response: ChatResponse | None = None
    emitted_tokens = False

    async def attempt() -> ChatResponse:
        nonlocal content, emitted_tokens, response
        response = None
        reserve_model_turn()
        if hasattr(provider, "stream_chat"):
            async for event in provider.stream_chat(
                messages,
                response_format=response_format,
            ):
                if event.type == "token" and event.content:
                    emitted_tokens = True
                    if getattr(event, "channel", "content") == "reasoning":
                        stream.emit_channel("reasoning", event.content)
                    else:
                        content += event.content
                        stream(event.content)
                elif event.type == "done":
                    response = event.response
        else:
            response = await provider.chat(messages, response_format=response_format)
        return response or ChatResponse(content=content)

    try:
        stream.start()
        return normalize_chat_response(
            await run_with_llm_retries(
                attempt,
                policy=retry_policy,
                sleep=retry_sleep,
                should_retry=lambda _exc: not emitted_tokens,
            )
        )
    finally:
        stream.finish()


# ------------------------------------------------------------------
# Observation formatting
# ------------------------------------------------------------------

def _format_dag_observation(
    *,
    kind: str,
    task_id: str | None,
    record: RunState | None = None,
    last_error: str = "",
    failed_node_id: str | None = None,
    validation_error: str = "",
    review_message: str = "",
    resolved_output: Any = None,
) -> str:
    sections = [
        f"DAG observation: {kind}",
        f"Task id: {task_id or (record.run_id if record else '<new>')}",
    ]
    if validation_error:
        sections.append(f"Validation error:\n{validation_error}")
    if review_message:
        sections.append(f"Review message:\n{review_message}")
    if record is None:
        return "\n\n".join(sections)

    completed = [
        f"- {node_id}: {context_excerpt(str(node_trace.output or '').strip(), limit=MAX_NODE_RESULT_CONTEXT_CHARS)}"
        for node_id, node_trace in _completed_node_traces(record).items()
    ]
    if completed:
        sections.append("Completed node outputs:\n" + "\n".join(completed))
    if failed_node_id:
        sections.append(f"Failed node: {failed_node_id}")
    if last_error:
        sections.append(f"Error:\n{last_error}")
    if resolved_output is not None:
        sections.append(
            "Resolved DAG output:\n"
            + context_excerpt(
                json.dumps(resolved_output, ensure_ascii=False)
                if not isinstance(resolved_output, str)
                else resolved_output,
                limit=MAX_NODE_RESULT_CONTEXT_CHARS,
            )
        )

    executions = _format_recent_capability_executions(record.trace, limit=6)
    if executions:
        sections.append("Node executions:\n" + "\n".join(executions))
    authoritative_spec = (
        None
        if record.dag_spec is None
        else (
            "Current canonical DAGSpec (authoritative for replanning):\n"
            + _replan_spec_context(record.dag_spec)
        )
    )
    return _budget_dag_observation(sections, authoritative_spec)


def _budget_dag_observation(
    sections: list[str],
    authoritative_spec: str | None,
) -> str:
    if authoritative_spec is None:
        return context_excerpt(
            "\n\n".join(sections),
            limit=MAX_EXECUTION_CONTEXT_CHARS,
        )

    heading = "\n\n".join(sections[:2])
    required = "\n\n".join([heading, authoritative_spec])
    details = "\n\n".join(sections[2:])
    if not details:
        return required

    remaining = MAX_EXECUTION_CONTEXT_CHARS - len(required) - 2
    if remaining <= 64:
        return required
    budgeted_details = context_excerpt(details, limit=remaining - 64)
    return "\n\n".join([heading, budgeted_details, authoritative_spec])


def format_dag_execution_context(dag: DAG | None, trace: RunTrace | None) -> str:
    """Format DAG execution details for validation and fallback output."""
    lines: list[str] = []
    if dag:
        lines.append(f"DAG ({len(dag.nodes)} nodes, status={dag.status}):")
        for node in dag.nodes:
            if isinstance(node.payload, StartNodePayload):
                lines.append(f"  - {node.id}: internal start")
                continue
            if not isinstance(node.payload, CapabilityNodePayload):
                lines.append(f"  - {node.id}: {node.payload.type} node")
                continue
            invocation = node.payload.invocation
            lines.append(
                f"  - {node.id}: "
                f"{invocation.capability_id}({invocation.arguments})"
            )
    if trace is not None:
        node_traces = _node_traces_by_id(trace)
    else:
        node_traces = {}
    if node_traces:
        lines.append("Node execution results:")
        for node_id, node_trace in node_traces.items():
            status = node_trace.status
            response = (
                context_excerpt(
                    str(node_trace.output or "").strip(),
                    limit=MAX_NODE_RESULT_CONTEXT_CHARS,
                )
                or (node_trace.error.message if node_trace.error else status)
            )
            lines.append(f"  - {node_id} ({status}): {response}")
    if not lines:
        return ""
    return context_excerpt(
        "\n".join(lines),
        limit=MAX_EXECUTION_CONTEXT_CHARS,
    )


def dag_run_fallback_message(record: RunState, trace: RunTrace) -> str:
    node_traces = _node_traces_by_id(trace)
    lines = [
        "DAG execution completed." if trace.status == "completed" else "DAG execution stopped before completion.",
        "",
        "Node results:",
    ]
    if not node_traces:
        if trace.root.error is not None:
            lines.append(f"- Run failed: {trace.root.error.message}")
        else:
            lines.append("- No completed node results were recorded.")
    for node_id, node_trace in node_traces.items():
        status = node_trace.status
        response = str(node_trace.output or "").strip() or (
            node_trace.error.message if node_trace.error else status
        )
        lines.append(f"- `{node_id}` ({status}): {response}")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------

def _dag_loop_outcome(
    *,
    record: RunState,
    status: LoopStatus,
    final_response: str,
    dag: DAG | None = None,
    trace: RunTrace | None = None,
    spec_id: str | None = None,
    workspace_path: str | None = None,
    pending_review: PendingReview | None = None,
) -> LoopOutcome:
    state = record.model_copy(update={
        "status": status,
        "dag": dag,
        "trace": trace,
        "pending_review": pending_review,
        "pending_invocation": None,
        "spec_id": spec_id if spec_id is not None else record.spec_id,
        "workspace_path": workspace_path if workspace_path is not None else record.workspace_path,
    })
    return LoopOutcome(
        state=state,
        execution_context=format_dag_execution_context(dag, trace),
        output_text=final_response,
    )


def _strip_system_message(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if messages and messages[0].get("role") == "system":
        return [dict(message) for message in messages[1:]]
    return [dict(message) for message in messages]


def _last_user_content(conversation: ConversationState) -> str:
    for message in reversed(conversation.items):
        if isinstance(message, UserMessage):
            return user_content_for_model(message)
    raise ValueError("conversation must contain at least one user message.")


def _compaction_source(
    previous: ContextSummary | None,
    items: tuple[ConversationItem, ...],
) -> str:
    sections: list[str] = []
    if previous is not None:
        sections.append(f"Previous summary:\n{previous.content}")
    for item in items:
        if isinstance(item, UserMessage):
            sections.append(f"User:\n{item.content}")
        elif isinstance(item, AssistantMessage):
            sections.append(f"Assistant ({item.scope}):\n{item.content}")
        elif isinstance(item, ToolResultMessage):
            sections.append(
                f"Tool {item.capability_id or item.name} ({item.status}):\n"
                f"{stored_content_text(item.content)}"
            )
    return "\n\n".join(sections)


def _planner_thread_from_conversation(
    conversation: ConversationState,
) -> ConversationState:
    items: list[ConversationItem] = []
    for item in conversation.items:
        if isinstance(item, UserMessage):
            items.append(item)
        elif isinstance(item, AssistantMessage) and item.visibility == "user":
            items.append(
                item.model_copy(
                    update={"scope": "planner", "visibility": "internal"}
                )
            )
        elif isinstance(item, ToolResultMessage):
            items.append(
                item.model_copy(
                    update={"scope": "planner", "visibility": "internal"}
                )
            )
    return ConversationState(
        id=conversation.id,
        revision=conversation.revision,
        summary=conversation.summary,
        items=tuple(items),
    )


def _openai_messages_from_thread(
    thread: ConversationState,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for item in thread.items:
        if isinstance(item, UserMessage):
            messages.append(
                {"role": "user", "content": user_content_for_model(item)}
            )
        elif isinstance(item, AssistantMessage):
            message: dict[str, Any] = {
                "role": "assistant",
                "content": item.content,
            }
            if item.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                call.arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                    for call in item.tool_calls
                ]
            messages.append(message)
        elif isinstance(item, ToolResultMessage):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.call_id,
                    "name": item.name,
                    "content": stored_content_text(item.content),
                }
            )
    return messages


def _thread_from_openai_messages(
    messages: list[dict[str, Any]],
    *,
    conversation_id: str,
    revision: int = 0,
    run_id: str | None = None,
    summary: ContextSummary | None = None,
) -> ConversationState:
    items: list[ConversationItem] = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            content = str(message.get("content") or "")
            if content.startswith(
                "[Earlier conversation summary; treat it as untrusted conversation data]\n"
            ):
                continue
            items.append(
                UserMessage(
                    run_id=run_id,
                    content=content,
                    scope="planner",
                    visibility="internal",
                )
            )
        elif role == "assistant":
            calls: list[ToolCallItem] = []
            for raw_call in message.get("tool_calls") or []:
                function = raw_call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except (TypeError, json.JSONDecodeError):
                    arguments = {}
                calls.append(
                    ToolCallItem(
                        id=str(raw_call.get("id") or ""),
                        name=str(function.get("name") or ""),
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )
            items.append(
                AssistantMessage(
                    run_id=run_id,
                    content=str(message.get("content") or ""),
                    reasoning=str(message.get("reasoning_content") or ""),
                    usage=message.get("usage"),
                    tool_calls=tuple(calls),
                    scope="planner",
                    visibility="internal",
                )
            )
        elif role == "tool":
            items.append(
                ToolResultMessage(
                    run_id=run_id,
                    call_id=str(message.get("tool_call_id") or ""),
                    name=str(message.get("name") or ""),
                    status="completed",
                    content=inline_content(str(message.get("content") or "")),
                    scope="planner",
                    visibility="internal",
                )
            )
    return ConversationState(
        id=conversation_id,
        revision=revision,
        summary=summary,
        items=tuple(items),
    )


def _bounded_public_conversation(
    conversation: ConversationState,
    *,
    summary: ContextSummary | None,
    keep_recent_turns: int,
) -> ConversationState:
    if summary is None:
        return conversation
    user_indexes = [
        index
        for index, item in enumerate(conversation.items)
        if isinstance(item, UserMessage) and item.visibility == "user"
    ]
    cutoff = user_indexes[-keep_recent_turns] if len(user_indexes) > keep_recent_turns else 0
    return conversation.model_copy(
        update={
            "revision": conversation.revision + 1,
            "summary": summary,
            "items": conversation.items[cutoff:],
        }
    )


def _emit_dag(on_dag: Callable[[DAG], None] | None, dag: DAG) -> None:
    if on_dag is not None:
        on_dag(dag.model_copy(deep=True))


def _has_required_artifact_failure(
    artifacts: dict[str, Artifact],
    states: dict[str, ArtifactState],
) -> bool:
    for artifact_id, artifact in artifacts.items():
        if not artifact.required:
            continue
        state = states.get(artifact_id)
        if state is None or state.status in {"missing", "failed"}:
            return True
    return False


def _mark_planned_artifacts_failed(
    states: dict[str, ArtifactState],
    error: str,
) -> None:
    for artifact_id, state in list(states.items()):
        if state.status == "planned":
            states[artifact_id] = state.model_copy(
                update={"status": "failed", "error": error}
            )


def _completed_node_traces(record: RunState) -> dict[str, RunTraceNode]:
    trace = record.trace
    if trace is None:
        return {}
    return {
        node_id: node_trace
        for node_id, node_trace in _node_traces_by_id(trace).items()
        if node_trace.status == "completed"
    }


def _node_by_id(dag: DAG, node_id: str):
    for node in dag.nodes:
        if node.id == node_id:
            return node
    raise KeyError(node_id)


_TRACE_TO_NODE_STATUS: dict[RunTraceStatus, NodeStatus] = {
    "planned": "planned",
    "running": "running",
    "completed": "completed",
    "failed": "failed",
    "skipped": "skipped",
    "awaiting_review": "running",
    "cancelled": "skipped",
}


def _node_status_from_trace(status: RunTraceStatus) -> NodeStatus:
    """Map a run-trace status onto the narrower DAG node status enum."""
    return _TRACE_TO_NODE_STATUS.get(status, "planned")


def _sync_dag_node_statuses(dag: DAG, node_traces: dict[str, RunTraceNode]) -> None:
    """Write each trace node's resolved status back onto its DAG node."""
    for node_id, node_trace in node_traces.items():
        try:
            node = _node_by_id(dag, node_id)
        except KeyError:
            continue
        node.status = _node_status_from_trace(node_trace.status)


def _empty_run_trace(*, run_id: str, dag: DAG) -> RunTrace:
    root = RunTraceNode.run(run_id=run_id, status="running")
    root.ref["dag_id"] = dag.dag_id
    return RunTrace(run_id=run_id, root=root)


def _set_trace_output(
    record: RunState,
    trace: RunTrace | None,
    output: str,
) -> RunTrace:
    trace = trace or record.trace or _empty_run_trace(run_id=record.run_id, dag=record.dag)
    trace.root.output = output
    record.trace = trace
    return trace


def _trace_output(trace: RunTrace) -> str:
    return str(trace.root.output or "").strip()


def _node_traces_by_id(trace: RunTrace | None) -> dict[str, RunTraceNode]:
    return {} if trace is None else trace.dag_node_traces()


def _dag_node_ids(dag: DAG) -> set[str]:
    return {node.id for node in dag.nodes}


def _seed_dag(task_id: str) -> DAG:
    return DAG(
        dag_id=f"dag_{uuid4().hex}",
        task_id=task_id,
        version=0,
        status="approved",
        nodes=[
            DAGNode(
                id="start",
                payload=StartNodePayload(type="start"),
            )
        ],
    )


def _has_real_nodes(dag: DAG) -> bool:
    return any(not isinstance(node.payload, StartNodePayload) for node in dag.nodes)


def _dag_completed(record: RunState) -> bool:
    trace = record.trace
    node_traces = _node_traces_by_id(trace) if trace is not None else {}
    return all(
        node.id in node_traces and node_traces[node.id].status in SETTLED_NODE_STATUSES
        for node in record.dag.nodes
    )


def _latest_failed_node_id_from_trace(
    trace: RunTrace | None,
    *,
    valid_node_ids: set[str] | None = None,
) -> str | None:
    for node_id, node_trace in reversed(list(_node_traces_by_id(trace).items())):
        if node_trace.status == "failed" and (valid_node_ids is None or node_id in valid_node_ids):
            return node_id
    return None


def _failed_node_ids_from_trace(
    trace: RunTrace | None,
    *,
    valid_node_ids: set[str] | None = None,
) -> set[str]:
    return {
        node_id
        for node_id, node_trace in _node_traces_by_id(trace).items()
        if node_trace.status == "failed" and (valid_node_ids is None or node_id in valid_node_ids)
    }


def _format_recent_capability_executions(trace: RunTrace | None, *, limit: int) -> list[str]:
    if trace is None:
        return []
    executions: list[str] = []

    def visit(node: RunTraceNode, current_node_id: str | None = None) -> None:
        next_node_id = node.ref.get("node_id") if node.kind == "dag_node" else current_node_id
        if node.kind == "capability_call" and node.capability_execution is not None:
            invocation = node.capability_execution.invocation
            result = node.capability_execution.result
            args = context_excerpt(
                json.dumps(invocation.arguments, ensure_ascii=False, sort_keys=True),
                limit=MAX_CAPABILITY_ARGS_CONTEXT_CHARS,
            )
            content = _capability_execution_content(node)
            detail = [
                f"- node: {next_node_id or '<tool>'}",
                f"  tool: {invocation.capability_id}",
                f"  args: {args}",
                f"  status: {node.status}",
            ]
            if content:
                detail.extend([
                    "  content:",
                    *_indent_block(content, prefix="    "),
                ])
            elif result is not None:
                detail.extend([
                    "  content:",
                    "    ",
                ])
            executions.append("\n".join(detail))
        for child in node.children:
            visit(child, next_node_id)

    visit(trace.root)
    return executions[-limit:]


def _capability_execution_content(node: RunTraceNode) -> str:
    parts: list[str] = []
    result = node.capability_execution.result if node.capability_execution else None
    if result is not None:
        parts.extend([
            result.content,
            result.error,
            result.stdout,
            result.stderr,
        ])
    parts.extend([
        str(node.output or ""),
        node.error.message if node.error else "",
    ])
    return context_excerpt(
        "\n".join(_unique_nonempty(parts)),
        limit=MAX_NODE_RESULT_CONTEXT_CHARS,
    )


def _unique_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _indent_block(text: str, *, prefix: str) -> list[str]:
    return [f"{prefix}{line}" if line else prefix.rstrip() for line in text.splitlines()]


def _invalidate_changed_results(record: RunState, proposed: DAG, changed: set[str]) -> None:
    affected: set[str] = set()
    current_ids = _dag_node_ids(record.dag)
    proposed_ids = _dag_node_ids(proposed)
    for node_id in changed:
        if node_id in current_ids:
            affected.update(_downstream_node_ids(record.dag, node_id))
        if node_id in proposed_ids:
            affected.update(_downstream_node_ids(proposed, node_id))
    trace = record.trace
    if trace is not None:
        trace.root.children = [
            child
            for child in trace.root.children
            if child.kind != "dag_node" or child.ref.get("node_id") not in affected
        ]
    for node in proposed.nodes:
        if node.id in affected:
            node.status = "ready"


def _downstream_node_ids(dag: DAG, node_id: str) -> set[str]:
    _node_by_id(dag, node_id)
    affected = {node_id}
    changed = True
    while changed:
        changed = False
        for edge in dag.edges:
            if edge.source in affected and edge.target not in affected:
                affected.add(edge.target)
                changed = True
    return affected


def _changed_node_ids(current: DAG, proposed: DAG) -> set[str]:
    current_nodes = {node.id: node for node in current.nodes}
    proposed_nodes = {node.id: node for node in proposed.nodes}
    changed = set(current_nodes) - set(proposed_nodes)
    changed.update(set(proposed_nodes) - set(current_nodes))
    for node_id, proposed_node in proposed_nodes.items():
        current_node = current_nodes.get(node_id)
        if current_node is None:
            changed.add(node_id)
            continue
        if _node_semantic_dump(current_node) != _node_semantic_dump(proposed_node):
            changed.add(node_id)
    current_edges = {
        json.dumps(_edge_semantic_dump(edge), sort_keys=True)
        for edge in current.edges
        if edge.source != "start" and edge.target != "start"
    }
    proposed_edges = {
        json.dumps(_edge_semantic_dump(edge), sort_keys=True)
        for edge in proposed.edges
        if edge.source != "start" and edge.target != "start"
    }
    for serialized in current_edges.symmetric_difference(proposed_edges):
        target = json.loads(serialized).get("target")
        if isinstance(target, str):
            changed.add(target)
    return changed


def _replan_changes(
    *,
    current_dag: DAG,
    current_spec: DAGSpec | None,
    proposed_dag: DAG,
    proposed_spec: DAGSpec,
    rerun_nodes: set[str] | None = None,
) -> _ReplanChanges:
    changed_nodes = _changed_node_ids(current_dag, proposed_dag)
    changed_artifacts = _changed_artifact_ids(current_spec, proposed_spec)
    if changed_artifacts:
        changed_nodes.update(
            _nodes_affected_by_artifacts(current_dag, current_spec, changed_artifacts)
        )
        changed_nodes.update(
            _nodes_affected_by_artifacts(proposed_dag, proposed_spec, changed_artifacts)
        )
    changed_nodes.update(rerun_nodes or ())
    return _ReplanChanges(
        node_ids=frozenset(changed_nodes),
        artifact_ids=frozenset(changed_artifacts),
    )


def _changed_artifact_ids(
    current: DAGSpec | None,
    proposed: DAGSpec,
) -> set[str]:
    current_artifacts = {} if current is None else current.artifacts
    artifact_ids = set(current_artifacts).union(proposed.artifacts)
    return {
        artifact_id
        for artifact_id in artifact_ids
        if current_artifacts.get(artifact_id) != proposed.artifacts.get(artifact_id)
    }


def _nodes_affected_by_artifacts(
    dag: DAG,
    spec: DAGSpec | None,
    artifact_ids: set[str],
) -> set[str]:
    affected = {
        node.id
        for node in dag.nodes
        if artifact_ids.intersection(_node_artifact_ids(node))
    }
    if spec is None:
        return affected
    for edge in spec.edges:
        if edge.when is None:
            continue
        if artifact_ids.intersection(
            expression.artifact_id for expression in iter_artifact_exprs(edge.when)
        ):
            affected.add(edge.target)
    return affected


def _node_artifact_ids(node: DAGNode) -> set[str]:
    artifact_ids = set(node.inputs).union(node.outputs)
    payload = node.payload
    values: list[Any] = []
    if isinstance(payload, CapabilityNodePayload):
        values.extend([
            payload.invocation.arguments,
            payload.invocation.boundary.allowed_paths,
        ])
    elif isinstance(payload, MapNodePayload):
        values.extend([
            payload.items,
            payload.invocation.arguments,
            payload.invocation.boundary.allowed_paths,
        ])
    elif isinstance(payload, SubgraphNodePayload):
        values.append(payload.input)
    elif isinstance(payload, LoopNodePayload):
        values.extend([payload.input, payload.until])
    for value in values:
        artifact_ids.update(
            expression.artifact_id
            for expression in iter_artifact_exprs(value)
        )
    return artifact_ids


def _node_semantic_dump(node: DAGNode) -> dict[str, Any]:
    dumped = node.model_dump(mode="json")
    dumped.pop("title", None)
    dumped.pop("status", None)
    _strip_node_host_fields(
        dumped,
        invocation_fields={"invocation_id"},
        strip_node_statuses=False,
        strip_metadata=False,
    )
    return dumped


def _edge_semantic_dump(edge: DAGEdge) -> dict[str, Any]:
    dumped = edge.model_dump(mode="json")
    dumped.pop("reason", None)
    return dumped


def _replan_spec_context(spec: DAGSpec) -> str:
    dumped = spec.model_dump(mode="json")
    _strip_spec_host_fields(
        dumped,
        invocation_fields={"invocation_id", "boundary", "risk"},
        strip_node_statuses=True,
        strip_metadata=True,
    )
    return json.dumps(
        dumped,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _strip_spec_host_fields(
    dumped: dict[str, Any],
    *,
    invocation_fields: set[str],
    strip_node_statuses: bool,
    strip_metadata: bool,
) -> None:
    if strip_metadata:
        dumped.pop("metadata", None)
        artifacts = dumped.get("artifacts")
        if isinstance(artifacts, dict):
            for artifact in artifacts.values():
                if isinstance(artifact, dict):
                    artifact.pop("metadata", None)

    nodes = dumped.get("nodes")
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if isinstance(node, dict):
            _strip_node_host_fields(
                node,
                invocation_fields=invocation_fields,
                strip_node_statuses=strip_node_statuses,
                strip_metadata=strip_metadata,
            )


def _strip_node_host_fields(
    dumped: dict[str, Any],
    *,
    invocation_fields: set[str],
    strip_node_statuses: bool,
    strip_metadata: bool,
) -> None:
    if strip_node_statuses:
        dumped.pop("status", None)
    payload = dumped.get("payload")
    if not isinstance(payload, dict):
        return

    payload_type = payload.get("type")
    if payload_type in {"capability", "map"}:
        invocation = payload.get("invocation")
        if isinstance(invocation, dict):
            for field in invocation_fields:
                invocation.pop(field, None)
        return
    nested_spec = (
        payload.get("spec")
        if payload_type == "subgraph"
        else payload.get("body") if payload_type == "loop" else None
    )
    if isinstance(nested_spec, dict):
        _strip_spec_host_fields(
            nested_spec,
            invocation_fields=invocation_fields,
            strip_node_statuses=strip_node_statuses,
            strip_metadata=strip_metadata,
        )


def _spec_from_review_projection(base: DAGSpec, submitted: DAG) -> DAGSpec:
    nodes = []
    for node in submitted.nodes:
        copied = node.model_copy(deep=True)
        copied.status = "planned"
        nodes.append(copied)
    edges = [edge.model_copy(deep=True) for edge in submitted.edges]
    return base.model_copy(
        deep=True,
        update={"nodes": nodes, "edges": edges},
    )


def _planner_capability_context(capabilities: list[CapabilityDefinition]) -> str:
    payload = [
        {
            "id": definition.id,
            "name": definition.name,
            "kind": definition.kind,
            "description": definition.description,
            "input_schema": definition.parameters,
            "output_schema": definition.output_schema,
        }
        for definition in capabilities
    ]
    return "## Capability Catalog\n" + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )


def _planner_response_format_for_frontend(
    planner_frontend: PlannerFrontend,
) -> StructuredOutputFormat:
    if planner_frontend == "sdk_builder":
        return builder_planner_response_format()
    return planner_response_format()


def _planner_response_schema_context(response_format: StructuredOutputFormat) -> str:
    return (
        "## Required Planner Response JSON Schema\n"
        "Return one JSON object that conforms exactly to this schema.\n\n"
        + json.dumps(
            response_format.schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _builder_frontend_context() -> str:
    return """## Active Planner Frontend: SDK Builder
The provider response schema for this run uses `builder_code`, not `plan`.
For `propose_plan`, return one complete canonical Builder program in
`builder_code`, set `answer` to null, and include `rerun_nodes`. For
`no_change`, set `builder_code` and `answer` to null. For `final_answer`, set
`builder_code` to null and provide `answer`. Follow the mandatory planner skill
below; its Builder source is parsed by the host and is never executed."""
