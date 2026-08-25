"""One-turn, non-executing DAG design over the runtime capability catalog."""

from __future__ import annotations

import json
from typing import Iterable
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from pydantic import ValidationError as PydanticValidationError

from dagent.harness_runtime.capability_scope import CapabilityScope
from dagent.harness_runtime.dag_agent import DAGAgent, _chat_for_dag
from dagent.harness_runtime.dag_builder import DAGCreationError, inspect_dag_spec
from dagent.harness_runtime.dynamic_planner import resolve_dag_spec_capabilities
from dagent.harness_runtime.runtime_events import LoopEventHandler
from dagent.harness_runtime.planner_schema import (
    dag_design_candidate_schema,
    dag_design_response_format,
    parse_dag_design_response,
)
from dagent.schemas import (
    AssistantMessage,
    CapabilityDefinition,
    CapabilityNodePayload,
    ConversationState,
    ConversationItem,
    DAGDesignAnswer,
    DAGDesignFailure,
    DAGDesignNoChange,
    DAGDesignProposal,
    DAGDesignResult,
    DAGDesignSelection,
    DAGDiagnostic,
    DAGNode,
    DAGSpec,
    LoopNodePayload,
    MapNodePayload,
    SubgraphNodePayload,
    UserMessage,
    iter_dag_invocations,
)


async def design_dag(
    designer: DAGAgent,
    instruction: str,
    *,
    capabilities: Iterable[CapabilityDefinition],
    capability_scope: CapabilityScope,
    current: DAGSpec | None = None,
    selection: DAGDesignSelection | None = None,
    conversation: ConversationState | None = None,
    on_event: LoopEventHandler | None = None,
) -> DAGDesignResult:
    """Ask the configured provider for one candidate without running the graph."""

    if not isinstance(instruction, str):
        raise TypeError("instruction must be a string.")
    normalized_instruction = instruction.strip()
    if not normalized_instruction:
        raise ValueError("instruction must be a non-empty string.")

    base_conversation = (conversation or ConversationState()).model_copy(deep=True)
    selection_diagnostic = _selection_diagnostic(current, selection)
    if selection_diagnostic is not None:
        return DAGDesignFailure(
            diagnostics=(selection_diagnostic,),
            conversation=base_conversation,
        )

    response_format = dag_design_response_format()
    system_message = designer._build_system_message(
        capability_scope,
        response_format=response_format,
        additional_context=(
            _design_mode_context(current=current, selection=selection),
        ),
        include_planner_frontend=False,
    )
    user_item = UserMessage(content=normalized_instruction, scope="planner")
    pending_conversation = _append_item(base_conversation, user_item)
    prepared = await designer.context_assembler.prepare(
        system_message=system_message,
        conversation=pending_conversation,
        policy=designer.context_policy,
    )
    response = await _chat_for_dag(
        designer.loop.provider,
        prepared.messages,
        retry_policy=designer.loop.llm_retry_policy,
        retry_sleep=designer.loop.llm_retry_sleep,
        response_format=response_format,
        on_event=on_event,
    )
    if on_event is not None:
        on_event({"type": "validating", "message": "Validating DAG design response."})

    def common(visible_content: str) -> dict[str, object]:
        return {
            "conversation": _append_item(
                prepared.conversation,
                AssistantMessage(
                    content=visible_content,
                    reasoning=response.reasoning_content,
                    refusal=response.refusal,
                    usage=response.usage,
                    scope="planner",
                ),
            ),
            "usage": response.usage,
            "context_usage": prepared.usage,
        }

    def failure(diagnostics: tuple[DAGDiagnostic, ...]) -> DAGDesignFailure:
        message = (
            "I couldn't complete the DAG design. "
            f"See diagnostic {diagnostics[0].code}."
        )
        return DAGDesignFailure(diagnostics=diagnostics, **common(message))

    def validation_passed() -> None:
        if on_event is not None:
            on_event({
                "type": "validation_passed",
                "summary": "DAG design response is valid.",
                "issues": [],
            })

    if response.refusal:
        return failure(
            (
                _error(
                    "dag_design.model_refusal",
                    f"DAG designer refused the request: {response.refusal}",
                ),
            )
        )

    try:
        design_response = parse_dag_design_response(response.content)
    except (ValueError, PydanticValidationError) as exc:
        return failure((_error("dag_design.invalid_model_output", str(exc)),))

    definitions = tuple(capabilities)
    if design_response.action == "final_answer":
        answer_diagnostics = _current_diagnostics(current, definitions)
        answer = str(design_response.answer or "").strip()
        if not answer_diagnostics:
            validation_passed()
        return DAGDesignAnswer(
            answer=answer,
            diagnostics=answer_diagnostics,
            **common(answer),
        )
    if design_response.action == "no_change":
        if current is None:
            return failure(
                (
                    _error(
                        "dag_design.no_current_dag",
                        "The designer returned no_change without a current DAGSpec.",
                    ),
                )
            )
        current_failure = _catalog_failure(current, definitions)
        if current_failure is not None:
            return failure((current_failure,))
        summary = str(design_response.summary or "").strip()
        validation_passed()
        return DAGDesignNoChange(
            summary=summary,
            **common(summary),
        )

    candidate_json = str(design_response.candidate_json or "")
    try:
        candidate = _parse_candidate(candidate_json)
    except (ValueError, PydanticValidationError, JSONSchemaValidationError) as exc:
        return failure((_candidate_parse_diagnostic(exc),))

    if current is not None:
        candidate.id = current.id
        candidate.version = current.version + 1
        _preserve_design_identity(candidate, current)

    _replace_model_invocation_ids(candidate)
    _reset_node_statuses(candidate)
    try:
        candidate = resolve_dag_spec_capabilities(candidate, definitions)
    except DAGCreationError as exc:
        return failure((_catalog_diagnostic(exc),))

    if current is not None:
        _preserve_invocation_ids(candidate, current)
        _preserve_node_statuses(candidate, current)
    diagnostics = inspect_dag_spec(candidate)
    if diagnostics:
        return failure(diagnostics)
    summary = str(design_response.summary or "").strip()
    validation_passed()
    return DAGDesignProposal(
        candidate=candidate,
        summary=summary,
        diagnostics=diagnostics,
        **common(summary),
    )


def _parse_candidate(content: str) -> DAGSpec:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"DAG candidate is not valid JSON: {exc.msg}.") from exc
    Draft202012Validator(dag_design_candidate_schema()).validate(payload)
    return DAGSpec.model_validate(payload)


def _design_mode_context(
    *,
    current: DAGSpec | None,
    selection: DAGDesignSelection | None,
) -> str:
    current_text = (
        "null"
        if current is None
        else json.dumps(
            current.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    selected_ids = [] if selection is None else list(selection.node_ids)
    current_diagnostics = (
        []
        if current is None
        else [
            diagnostic.model_dump(mode="json")
            for diagnostic in inspect_dag_spec(current)
        ]
    )
    schema_text = json.dumps(
        dag_design_candidate_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"""## Non-Executing DAG Design Mode
This turn designs, modifies, checks, or explains a DAG. It never executes the
graph. The response schema in this request overrides execution-planner response
field names described by the base profile.

- Use `propose_plan` with `candidate_json` containing one JSON-serialized,
  complete DAGSpec and a brief `summary`.
- Use `no_change` only when a current DAGSpec exists and needs no modification.
- Use `final_answer` for a question or explanation that needs no candidate.
- Preserve the current spec id, unchanged node ids, invocation ids, fields, and
  ordering. Preserve unchanged edges and their ordering. Include every retained
  node, edge, artifact, top-level field, and nested DAG in the complete candidate.
- Treat the selected node ids as a focus hint, not permission to omit other
  graph content.
- Use only capability ids in the injected catalog. Catalog metadata is
  authoritative; model-provided kind, risk, and boundary values are ignored.
- Do not claim to execute handlers or create runs, reviews, checkpoints,
  workspace files, or capability results. A candidate may still declare
  DAGSpec artifacts when its dataflow requires them.

Selected node ids: {json.dumps(selected_ids, ensure_ascii=False)}

Current DAGSpec:
{current_text}

Deterministic current-DAG diagnostics:
{json.dumps(current_diagnostics, ensure_ascii=False, separators=(",", ":"))}

## DAGSpec Schema For candidate_json
After decoding the string, candidate_json must conform exactly to this schema:
{schema_text}"""


def _selection_diagnostic(
    current: DAGSpec | None,
    selection: DAGDesignSelection | None,
) -> DAGDiagnostic | None:
    if selection is None or not selection.node_ids:
        return None
    if current is None:
        return _error(
            "dag_design.selection_without_current",
            "Selected node ids require a current DAGSpec.",
            path=("selection", "node_ids"),
        )
    known = {node.id for node in current.nodes}
    unknown = sorted(set(selection.node_ids) - known)
    if not unknown:
        return None
    return _error(
        "dag_design.selection_unknown_node",
        f"Selected node ids are not present in the current DAGSpec: {', '.join(unknown)}.",
        path=("selection", "node_ids"),
    )


def _catalog_failure(
    spec: DAGSpec,
    capabilities: tuple[CapabilityDefinition, ...],
) -> DAGDiagnostic | None:
    deterministic = inspect_dag_spec(spec)
    if deterministic:
        diagnostic = deterministic[0]
        return diagnostic.model_copy(
            update={"code": f"dag_design.current.{diagnostic.code}"}
        )
    try:
        resolve_dag_spec_capabilities(spec, capabilities)
    except DAGCreationError as exc:
        return _catalog_diagnostic(exc)
    return None


def _current_diagnostics(
    spec: DAGSpec | None,
    capabilities: tuple[CapabilityDefinition, ...],
) -> tuple[DAGDiagnostic, ...]:
    if spec is None:
        return ()
    diagnostic = _catalog_failure(spec, capabilities)
    return () if diagnostic is None else (diagnostic,)


def _catalog_diagnostic(exc: Exception) -> DAGDiagnostic:
    message = str(exc)
    if message.startswith("Unknown capability '"):
        code = "dag_design.capability_unavailable"
    elif "invalid arguments" in message:
        code = "dag_design.capability_arguments_invalid"
    else:
        code = "dag_design.candidate.invalid"
    return _error(code, message)


def _candidate_parse_diagnostic(exc: Exception) -> DAGDiagnostic:
    path: tuple[str | int, ...] = ()
    if isinstance(exc, JSONSchemaValidationError):
        path = tuple(exc.absolute_path)
    elif isinstance(exc, PydanticValidationError) and exc.errors():
        path = tuple(exc.errors()[0].get("loc", ()))
    return _error(
        "dag_design.invalid_candidate",
        str(exc),
        path=path,
    )


def _preserve_design_identity(candidate: DAGSpec, current: DAGSpec) -> None:
    candidate_nodes = {node.id: node for node in candidate.nodes}
    retained_nodes = [
        candidate_nodes[node.id] for node in current.nodes if node.id in candidate_nodes
    ]
    retained_ids = {node.id for node in retained_nodes}
    candidate.nodes = [
        *retained_nodes,
        *(node for node in candidate.nodes if node.id not in retained_ids),
    ]
    current_nodes = {node.id: node for node in current.nodes}
    for node in candidate.nodes:
        current_node = current_nodes.get(node.id)
        if current_node is None:
            continue
        candidate_payload = node.payload
        current_payload = current_node.payload
        if isinstance(candidate_payload, SubgraphNodePayload) and isinstance(
            current_payload,
            SubgraphNodePayload,
        ):
            _preserve_design_identity(candidate_payload.spec, current_payload.spec)
        elif isinstance(candidate_payload, LoopNodePayload) and isinstance(
            current_payload,
            LoopNodePayload,
        ):
            _preserve_design_identity(candidate_payload.body, current_payload.body)

    remaining_edges = list(candidate.edges)
    preserved_edges = []
    for current_edge in current.edges:
        match_index = next(
            (
                index
                for index, proposed_edge in enumerate(remaining_edges)
                if proposed_edge == current_edge
            ),
            None,
        )
        if match_index is None:
            continue
        preserved_edges.append(current_edge.model_copy(deep=True))
        remaining_edges.pop(match_index)
    candidate.edges = [*preserved_edges, *remaining_edges]

    candidate_artifacts = candidate.artifacts
    candidate.artifacts = {
        **{
            artifact_id: candidate_artifacts[artifact_id]
            for artifact_id in current.artifacts
            if artifact_id in candidate_artifacts
        },
        **{
            artifact_id: artifact
            for artifact_id, artifact in candidate_artifacts.items()
            if artifact_id not in current.artifacts
        },
    }


def _replace_model_invocation_ids(spec: DAGSpec) -> None:
    for invocation in iter_dag_invocations(spec.nodes):
        invocation.invocation_id = f"run_inv_{uuid4().hex}"


def _reset_node_statuses(spec: DAGSpec) -> None:
    for node in spec.nodes:
        node.status = "planned"
        payload = node.payload
        if isinstance(payload, SubgraphNodePayload):
            _reset_node_statuses(payload.spec)
        elif isinstance(payload, LoopNodePayload):
            _reset_node_statuses(payload.body)


def _preserve_invocation_ids(candidate: DAGSpec, current: DAGSpec) -> None:
    current_nodes = {node.id: node for node in current.nodes}
    for node in candidate.nodes:
        current_node = current_nodes.get(node.id)
        if current_node is None:
            continue
        _preserve_node_invocation_id(node, current_node)


def _preserve_node_invocation_id(candidate: DAGNode, current: DAGNode) -> None:
    candidate_payload = candidate.payload
    current_payload = current.payload
    if isinstance(
        candidate_payload, (CapabilityNodePayload, MapNodePayload)
    ) and isinstance(
        current_payload,
        type(candidate_payload),
    ):
        proposed = candidate_payload.invocation
        existing = current_payload.invocation
        if (
            proposed.capability_id == existing.capability_id
            and proposed.arguments == existing.arguments
        ):
            proposed.invocation_id = existing.invocation_id
        return
    if isinstance(candidate_payload, SubgraphNodePayload) and isinstance(
        current_payload,
        SubgraphNodePayload,
    ):
        _preserve_invocation_ids(candidate_payload.spec, current_payload.spec)
    elif isinstance(candidate_payload, LoopNodePayload) and isinstance(
        current_payload,
        LoopNodePayload,
    ):
        _preserve_invocation_ids(candidate_payload.body, current_payload.body)


def _preserve_node_statuses(candidate: DAGSpec, current: DAGSpec) -> None:
    current_nodes = {node.id: node for node in current.nodes}
    for node in candidate.nodes:
        current_node = current_nodes.get(node.id)
        if current_node is None:
            continue
        node.status = current_node.status
        candidate_payload = node.payload
        current_payload = current_node.payload
        if isinstance(candidate_payload, SubgraphNodePayload) and isinstance(
            current_payload,
            SubgraphNodePayload,
        ):
            _preserve_node_statuses(candidate_payload.spec, current_payload.spec)
        elif isinstance(candidate_payload, LoopNodePayload) and isinstance(
            current_payload,
            LoopNodePayload,
        ):
            _preserve_node_statuses(candidate_payload.body, current_payload.body)


def _append_item(
    conversation: ConversationState,
    item: ConversationItem,
) -> ConversationState:
    return conversation.model_copy(
        update={
            "revision": conversation.revision + 1,
            "items": (*conversation.items, item),
        }
    )


def _error(
    code: str,
    message: str,
    *,
    path: tuple[str | int, ...] = (),
) -> DAGDiagnostic:
    return DAGDiagnostic(
        severity="error",
        code=code,
        message=message,
        path=path,
    )
