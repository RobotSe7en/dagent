from __future__ import annotations

import json

import pytest

import dagent
from dagent.harness_runtime.dag_builder import DAGValidationError
from dagent.providers import ChatResponse, MockProvider
from dagent.schemas import (
    Artifact,
    CapabilityInvocation,
    DAGNode,
    DAGEdge,
    ModelTokenUsage,
)


def _design_response(
    spec: dagent.DAGSpec,
    *,
    summary: str = "Created a candidate DAG.",
) -> ChatResponse:
    return ChatResponse(
        content=json.dumps(
            {
                "action": "propose_plan",
                "candidate_json": json.dumps(spec.model_dump(mode="json")),
                "answer": None,
                "summary": summary,
            }
        ),
        usage=ModelTokenUsage(input_tokens=10, output_tokens=20, total_tokens=30),
    )


def _no_change_response() -> ChatResponse:
    return ChatResponse(
        content=json.dumps(
            {
                "action": "no_change",
                "candidate_json": None,
                "answer": None,
                "summary": "The current DAG already satisfies the instruction.",
            }
        )
    )


def _answer_response(answer: str) -> ChatResponse:
    return ChatResponse(
        content=json.dumps(
            {
                "action": "final_answer",
                "candidate_json": None,
                "answer": answer,
                "summary": None,
            }
        )
    )


def _node(
    node_id: str,
    capability_id: str,
    *,
    text: str,
    invocation_id: str | None = None,
    kind: str = "tool",
    risk: str = "low",
    boundary: dagent.Boundary | None = None,
) -> DAGNode:
    invocation = CapabilityInvocation(
        capability_id=capability_id,
        kind=kind,
        risk=risk,
        boundary=boundary or dagent.Boundary(),
        arguments={"text": text},
    )
    if invocation_id is not None:
        invocation.invocation_id = invocation_id
    return DAGNode(
        id=node_id,
        title=node_id.replace("_", " ").title(),
        payload={"type": "capability", "invocation": invocation},
    )


def _spec(
    capability_id: str,
    *,
    spec_id: str = "draft",
    text: str = "hello",
) -> dagent.DAGSpec:
    return dagent.DAGSpec(
        id=spec_id,
        name="Draft",
        description="A design candidate.",
        nodes=[_node("write", capability_id, text=text)],
    )


@pytest.mark.asyncio
async def test_design_dag_creates_candidate_without_execution_state_or_handler_call(
    tmp_path,
) -> None:
    calls: list[str] = []

    @dagent.tool
    def echo(text: str) -> str:
        calls.append(text)
        return text

    provider = MockProvider([_design_response(_spec("tool.echo"))])
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        capabilities=[echo],
    )

    result = await runner.design_dag(
        "Create a one-step echo DAG.",
        agent=dagent.DagAgent(capabilities=["tool.echo"]),
    )

    assert isinstance(result, dagent.DAGDesignProposal)
    assert result.type == "proposal"
    assert result.candidate.id == "draft"
    assert result.summary == "Created a candidate DAG."
    assert result.usage == ModelTokenUsage(
        input_tokens=10,
        output_tokens=20,
        total_tokens=30,
    )
    assert result.context_usage is not None
    assert len(result.conversation.items) == 2
    assert calls == []
    assert runner.runtime.session.runs == {}
    assert runner._run_checkpoints == {}
    assert provider.requests[0]["tools"] == []
    assert provider.requests[0]["response_format"].name == "dagent_dag_design_response"

    executable = dagent.Dag("execute_after_design")
    executable.add_node(dagent.Node("echo", target=echo, inputs={"text": "now"}))
    run_result = await runner.run(executable)
    assert run_result.status == "completed"
    assert calls == ["now"]
    runner.close()


@pytest.mark.asyncio
async def test_design_dag_modifies_complete_spec_and_preserves_stable_content(
    tmp_path,
) -> None:
    @dagent.tool(risk="medium")
    def echo(text: str) -> str:
        return text

    current = dagent.DAGSpec(
        id="workflow",
        name="Workflow",
        version=7,
        description="Keep this description.",
        input_schema={
            "type": "object",
            "properties": {"topic": {"type": "string"}},
            "required": ["topic"],
            "additionalProperties": False,
        },
        artifacts={
            "report": Artifact(
                id="report",
                paths=["outputs/report.md"],
                description="Keep artifact fields.",
                metadata={"owner": "sdk-user"},
            )
        },
        nodes=[
            _node("first", "tool.echo", text="one", invocation_id="run_inv_first"),
            _node("second", "tool.echo", text="two", invocation_id="run_inv_second"),
            _node("third", "tool.echo", text="three", invocation_id="run_inv_third"),
        ],
        edges=[
            DAGEdge(source="first", target="second", reason="first edge"),
            DAGEdge(source="second", target="third", reason="second edge"),
        ],
        output={
            "$expr": {
                "type": "node_output",
                "node_id": "third",
                "field": "value",
                "path": [],
            }
        },
        metadata={"project": "neutral", "nested": {"keep": True}},
    )
    current.nodes[0].status = "completed"
    candidate = current.model_copy(deep=True)
    candidate.id = "model_replaced_id"
    candidate.version = 999
    candidate.nodes = list(reversed(candidate.nodes))
    candidate.edges = list(reversed(candidate.edges))
    candidate.nodes[1].payload.invocation.arguments["text"] = "updated"
    for node in candidate.nodes:
        node.payload.invocation.invocation_id = f"model_{node.id}"

    provider = MockProvider(
        [_design_response(candidate, summary="Updated the middle step.")]
    )
    runner = dagent.Runner(workspace=tmp_path, provider=provider, capabilities=[echo])

    result = await runner.design_dag(
        "Update only the second step.",
        agent=dagent.DagAgent(capabilities=["tool.echo"]),
        current=current,
        selection=dagent.DAGDesignSelection(node_ids=("second",)),
    )

    assert isinstance(result, dagent.DAGDesignProposal)
    proposed = result.candidate
    assert proposed.id == "workflow"
    assert proposed.version == 8
    assert proposed.name == current.name
    assert proposed.description == current.description
    assert proposed.input_schema == current.input_schema
    assert proposed.artifacts == current.artifacts
    assert proposed.output == current.output
    assert proposed.metadata == current.metadata
    assert [node.id for node in proposed.nodes] == ["first", "second", "third"]
    assert [(edge.source, edge.target, edge.reason) for edge in proposed.edges] == [
        ("first", "second", "first edge"),
        ("second", "third", "second edge"),
    ]
    invocations = {node.id: node.payload.invocation for node in proposed.nodes}
    assert invocations["first"].invocation_id == "run_inv_first"
    assert invocations["third"].invocation_id == "run_inv_third"
    assert invocations["second"].invocation_id != "run_inv_second"
    assert invocations["second"].arguments == {"text": "updated"}
    assert proposed.nodes[0].status == "completed"
    runner.close()


@pytest.mark.asyncio
async def test_design_dag_returns_no_change_and_answer(tmp_path) -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return text

    provider = MockProvider(
        [
            _no_change_response(),
            _answer_response("The graph has one echo step."),
        ]
    )
    runner = dagent.Runner(workspace=tmp_path, provider=provider, capabilities=[echo])
    agent = dagent.DagAgent(capabilities=["tool.echo"])

    unchanged = await runner.design_dag(
        "Keep it as is.",
        agent=agent,
        current=_spec("tool.echo"),
    )
    answer = await runner.design_dag(
        "Explain the graph shape.",
        agent=agent,
    )

    assert isinstance(unchanged, dagent.DAGDesignNoChange)
    assert unchanged.type == "no_change"
    assert isinstance(answer, dagent.DAGDesignAnswer)
    assert answer.answer == "The graph has one echo step."
    runner.close()


@pytest.mark.asyncio
async def test_design_dag_reports_invalid_model_and_candidate_output(tmp_path) -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return text

    cyclic = dagent.DAGSpec(
        id="cycle",
        name="Cycle",
        nodes=[
            _node("a", "tool.echo", text="a"),
            _node("b", "tool.echo", text="b"),
        ],
        edges=[
            DAGEdge(source="a", target="b"),
            DAGEdge(source="b", target="a"),
        ],
    )
    provider = MockProvider(
        [
            ChatResponse(content="not-json"),
            _design_response(cyclic),
        ]
    )
    runner = dagent.Runner(workspace=tmp_path, provider=provider, capabilities=[echo])
    agent = dagent.DagAgent(capabilities=["tool.echo"])

    malformed = await runner.design_dag("Create it.", agent=agent)
    invalid = await runner.design_dag("Create a cycle.", agent=agent)

    assert isinstance(malformed, dagent.DAGDesignFailure)
    assert malformed.diagnostics[0].code == "dag_design.invalid_model_output"
    assert isinstance(invalid, dagent.DAGDesignFailure)
    assert invalid.diagnostics[0].code.startswith("dag_design.candidate.")
    assert "acyclic" in invalid.diagnostics[0].message.lower()
    runner.close()


@pytest.mark.asyncio
async def test_design_dag_rejects_unknown_and_out_of_scope_capabilities(
    tmp_path,
) -> None:
    @dagent.tool
    def allowed(text: str) -> str:
        return text

    @dagent.tool
    def outside(text: str) -> str:
        return text

    provider = MockProvider(
        [
            _design_response(_spec("tool.missing")),
            _design_response(_spec("tool.outside")),
        ]
    )
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        capabilities=[allowed, outside],
    )
    agent = dagent.DagAgent(capabilities=["tool.allowed"])

    unknown = await runner.design_dag("Use a missing tool.", agent=agent)
    out_of_scope = await runner.design_dag("Use the hidden tool.", agent=agent)

    assert isinstance(unknown, dagent.DAGDesignFailure)
    assert unknown.diagnostics[0].code == "dag_design.capability_unavailable"
    assert isinstance(out_of_scope, dagent.DAGDesignFailure)
    assert out_of_scope.diagnostics[0].code == "dag_design.capability_unavailable"
    catalog_text = provider.requests[1]["messages"][0]["content"]
    assert "tool.allowed" in catalog_text
    assert "tool.outside" not in catalog_text
    runner.close()


@pytest.mark.asyncio
async def test_design_dag_catalog_overrides_model_capability_metadata(tmp_path) -> None:
    @dagent.tool(risk="medium")
    def echo(text: str) -> str:
        return text

    candidate = dagent.DAGSpec(
        id="metadata",
        name="Metadata",
        nodes=[
            _node(
                "write",
                "tool.echo",
                text="hello",
                kind="mcp",
                risk="high",
                boundary=dagent.Boundary(allowed_paths=["model/owned"]),
            )
        ],
    )
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=MockProvider([_design_response(candidate)]),
        capabilities=[echo],
    )

    result = await runner.design_dag(
        "Create it.",
        agent=dagent.DagAgent(capabilities=["tool.echo"]),
    )

    assert isinstance(result, dagent.DAGDesignProposal)
    invocation = result.candidate.nodes[0].payload.invocation
    assert invocation.kind == "tool"
    assert invocation.risk == "medium"
    assert invocation.boundary == dagent.Boundary(allowed_paths=["."])
    runner.close()


@pytest.mark.asyncio
async def test_design_conversations_are_isolated_and_failures_do_not_mutate_inputs(
    tmp_path,
) -> None:
    provider = MockProvider(
        [
            _answer_response("First answer."),
            ChatResponse(content="invalid"),
            _answer_response("Separate answer."),
        ]
    )
    runner = dagent.Runner(workspace=tmp_path, provider=provider)

    first = await runner.design_dag("First question.")
    first_snapshot = first.conversation.model_copy(deep=True)
    failed = await runner.design_dag(
        "Follow-up question.",
        conversation=first.conversation,
    )
    separate = await runner.design_dag("Separate question.")

    assert isinstance(first, dagent.DAGDesignAnswer)
    assert isinstance(failed, dagent.DAGDesignFailure)
    assert first.conversation == first_snapshot
    assert len(first.conversation.items) == 2
    assert len(failed.conversation.items) == 4
    assert separate.conversation.id != first.conversation.id
    second_messages = provider.requests[1]["messages"]
    assert any(
        message.get("content") == "First question." for message in second_messages
    )
    third_messages = provider.requests[2]["messages"]
    assert all(
        message.get("content") != "First question." for message in third_messages
    )
    runner.close()


def test_inspect_dag_spec_is_deterministic_and_preserves_validate_contract() -> None:
    spec = dagent.DAGSpec(
        id="invalid",
        name="Invalid",
        nodes=[_node("write", "tool.echo", text="hello")],
    )
    spec.nodes[0].inputs = ["missing"]

    first = dagent.inspect_dag_spec(spec)
    second = dagent.inspect_dag_spec(spec)

    assert first == second
    assert first[0].severity == "error"
    assert first[0].code == "dag.artifact.invalid"
    assert first[0].node_id == "write"
    assert first[0].path == ("nodes", "write")
    with pytest.raises(DAGValidationError, match="references unknown artifact"):
        dagent.validate_dag_spec(spec)
