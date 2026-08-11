"""Static DAG control flow: conditional edges, map fan-out, subgraphs, and loops."""

import asyncio

import pytest
from pydantic import ValidationError

import dagent
from dagent.harness_runtime.dag_builder import DAGValidationError, validate_dag_spec
from dagent.providers import ChatResponse, MockProvider


def run(coro):
    return asyncio.run(coro)


def run_dag(dag, graph_input, capabilities):
    runner = dagent.Runner(
        workspace=".",
        runtime_directory=".runtime",
        provider=MockProvider([]),
        capabilities=capabilities,
    )
    try:
        return run(runner.run(dag, graph_input=graph_input))
    finally:
        runner.close()


@dagent.tool
def score(text: str) -> dict:
    """Score a text."""

    return {"score": 0.9 if "good" in text else 0.1}


@dagent.tool
def publish(content: str) -> str:
    """Publish content."""

    return f"published:{content}"


@dagent.tool
def revise(content: str) -> str:
    """Revise content."""

    return f"revised:{content}"


@dagent.tool
def render(a: str | None = None, b: str | None = None) -> str:
    """Join two upstream results."""

    return f"{a}|{b}"


def _branching_dag() -> dagent.Dag:
    dag = dagent.Dag("branching", input=str)
    score_node = dagent.Node("score", target=score, inputs={"text": dag.input})
    publish_node = dagent.Node("publish", target=publish, inputs={"content": dag.input})
    revise_node = dagent.Node("revise", target=revise, inputs={"content": dag.input})
    join_node = dagent.Node(
        "join",
        target=render,
        inputs={"a": publish_node.output, "b": revise_node.output},
    )
    dag.add_node(score_node)
    dag.add_node(publish_node)
    dag.add_node(revise_node)
    dag.add_node(join_node)
    dag.add_edge(score_node, publish_node, when=score_node.output["score"] >= 0.8)
    dag.add_edge(score_node, revise_node, when=score_node.output["score"] < 0.8)
    dag.add_edge(publish_node, join_node)
    dag.add_edge(revise_node, join_node)
    return dag


def test_conditional_edge_takes_passing_branch_and_skips_other() -> None:
    result = run_dag(_branching_dag(), "good text", [score, publish, revise, render])

    assert result.status == "completed"
    assert result.node_value("publish") == "published:good text"
    assert result.trace.dag_node_traces()["revise"].status == "skipped"
    assert result.node_value("join") == "published:good text|None"


def test_conditional_edge_takes_other_branch_on_low_score() -> None:
    result = run_dag(_branching_dag(), "bad text", [score, publish, revise, render])

    assert result.status == "completed"
    assert result.node_value("revise") == "revised:bad text"
    assert result.trace.dag_node_traces()["publish"].status == "skipped"
    assert result.node_value("join") == "None|revised:bad text"


def test_skip_cascades_through_downstream_only_nodes() -> None:
    dag = dagent.Dag("cascade", input=str)
    score_node = dagent.Node("score", target=score, inputs={"text": dag.input})
    publish_node = dagent.Node("publish", target=publish, inputs={"content": dag.input})
    announce_node = dagent.Node("announce", target=publish, inputs={"content": publish_node.output})
    dag.add_node(score_node)
    dag.add_node(publish_node)
    dag.add_node(announce_node)
    dag.add_edge(score_node, publish_node, when=score_node.output["score"] >= 0.8)
    dag.add_edge(publish_node, announce_node)

    result = run_dag(dag, "bad text", [score, publish])

    assert result.status == "completed"
    node_traces = result.trace.dag_node_traces()
    assert node_traces["publish"].status == "skipped"
    assert node_traces["announce"].status == "skipped"


def test_truthy_edge_condition_without_compare() -> None:
    @dagent.tool
    def flag(text: str) -> dict:
        return {"go": bool(text)}

    dag = dagent.Dag("truthy", input=str)
    flag_node = dagent.Node("flag", target=flag, inputs={"text": dag.input})
    publish_node = dagent.Node("publish", target=publish, inputs={"content": dag.input})
    dag.add_node(flag_node)
    dag.add_node(publish_node)
    dag.add_edge(flag_node, publish_node, when=flag_node.output["go"])

    result = run_dag(dag, "go", [flag, publish])

    assert result.node_value("publish") == "published:go"


def test_edge_condition_must_reference_upstream_nodes() -> None:
    dag = dagent.Dag("invalid_when", input=str)
    a = dagent.Node("a", target=publish, inputs={"content": dag.input})
    b = dagent.Node("b", target=publish, inputs={"content": dag.input})
    c = dagent.Node("c", target=publish, inputs={"content": dag.input})
    dag.add_node(a)
    dag.add_node(b)
    dag.add_node(c)
    dag.add_edge(a, b, when=c.output == "published:x")
    dag.add_edge(a, c)

    with pytest.raises(DAGValidationError, match="condition reads output from node 'c'"):
        validate_dag_spec(dag.to_dag_spec())


def _condition_branching_dag() -> dagent.Dag:
    dag = dagent.Dag("condition_branching", input=str)
    score_node = dagent.Node("score", target=score, inputs={"text": dag.input})
    route = dagent.ConditionNode(
        "route",
        cases=[dagent.Case("publish", score_node.output["score"] >= 0.8)],
        default_branch="revise",
    )
    publish_node = dagent.Node("publish", target=publish, inputs={"content": dag.input})
    revise_node = dagent.Node("revise", target=revise, inputs={"content": dag.input})
    join_node = dagent.Node(
        "join",
        target=render,
        inputs={"a": publish_node.output, "b": revise_node.output},
    )
    for node in (score_node, route, publish_node, revise_node, join_node):
        dag.add_node(node)
    dag.add_edge(score_node, route)
    dag.add_edge(route, publish_node, branch="publish")
    dag.add_edge(route, revise_node, branch="revise")
    dag.add_edge(publish_node, join_node)
    dag.add_edge(revise_node, join_node)
    return dag


def test_condition_node_selects_case_and_records_branch() -> None:
    result = run_dag(
        _condition_branching_dag(),
        "good text",
        [score, publish, revise, render],
    )

    route_trace = result.trace.dag_node_traces()["route"]
    assert route_trace.selected_branch == "publish"
    assert result.node_value("route") == {"branch": "publish"}
    assert result.node_value("publish") == "published:good text"
    assert result.trace.dag_node_traces()["revise"].status == "skipped"
    assert result.node_value("join") == "published:good text|None"


def test_condition_node_selects_default_branch() -> None:
    result = run_dag(
        _condition_branching_dag(),
        "bad text",
        [score, publish, revise, render],
    )

    assert result.trace.dag_node_traces()["route"].selected_branch == "revise"
    assert result.node_value("revise") == "revised:bad text"
    assert result.trace.dag_node_traces()["publish"].status == "skipped"


def test_condition_node_uses_first_matching_case_and_logical_expressions() -> None:
    dag = dagent.Dag("ordered_condition", input=str)
    score_node = dagent.Node("score", target=score, inputs={"text": dag.input})
    route = dagent.ConditionNode(
        "route",
        cases=[
            dagent.Case(
                "first",
                dagent.all_of(
                    score_node.output["score"] >= 0.5,
                    dagent.any_of(
                        score_node.output["score"] == 0.9,
                        score_node.output["score"] == 1.0,
                    ),
                    dagent.not_(score_node.output["score"] < 0.5),
                ),
            ),
            dagent.Case("second", score_node.output["score"] >= 0.8),
        ],
        default_branch="none",
    )
    first = dagent.Node("first", target=publish, inputs={"content": "first"})
    second = dagent.Node("second", target=publish, inputs={"content": "second"})
    for node in (score_node, route, first, second):
        dag.add_node(node)
    dag.add_edge(score_node, route)
    dag.add_edge(route, first, branch="first")
    dag.add_edge(route, second, branch="second")

    result = run_dag(dag, "good text", [score, publish])

    assert result.trace.dag_node_traces()["route"].selected_branch == "first"
    assert result.node_value("first") == "published:first"
    assert result.trace.dag_node_traces()["second"].status == "skipped"


def test_condition_branch_can_fan_out() -> None:
    dag = dagent.Dag("condition_fanout", input=str)
    route = dagent.ConditionNode(
        "route",
        cases=[dagent.Case("go", dag.input == "go")],
        default_branch="stop",
    )
    first = dagent.Node("first", target=publish, inputs={"content": "a"})
    second = dagent.Node("second", target=publish, inputs={"content": "b"})
    for node in (route, first, second):
        dag.add_node(node)
    dag.add_edge(route, first, branch="go")
    dag.add_edge(route, second, branch="go")

    result = run_dag(dag, "go", [publish])

    assert result.node_value("first") == "published:a"
    assert result.node_value("second") == "published:b"


def test_unconnected_condition_branch_ends_path() -> None:
    dag = dagent.Dag("condition_early_end", input=str)
    route = dagent.ConditionNode(
        "route",
        cases=[dagent.Case("continue", dag.input == "go")],
        default_branch="end",
    )
    publish_node = dagent.Node("publish", target=publish, inputs={"content": dag.input})
    dag.add_node(route)
    dag.add_node(publish_node)
    dag.add_edge(route, publish_node, branch="continue")

    result = run_dag(dag, "stop", [publish])

    assert result.status == "completed"
    assert result.trace.dag_node_traces()["route"].selected_branch == "end"
    assert result.trace.dag_node_traces()["publish"].status == "skipped"


@pytest.mark.parametrize(
    ("edge_kwargs", "message"),
    [
        ({}, "must declare a branch"),
        ({"branch": "unknown"}, "unknown branch 'unknown'"),
        (
            {"branch": "go", "when": dagent.InputRef().as_expr()},
            "cannot declare both when and branch",
        ),
    ],
)
def test_condition_outgoing_edge_validation(edge_kwargs, message) -> None:
    dag = dagent.Dag("invalid_condition_edge", input=bool)
    route = dagent.ConditionNode(
        "route",
        cases=[dagent.Case("go", dag.input)],
        default_branch="stop",
    )
    target = dagent.Node("target", target=publish, inputs={"content": "x"})
    dag.add_node(route)
    dag.add_node(target)
    dag.add_edge(route, target, **edge_kwargs)

    with pytest.raises(DAGValidationError, match=message):
        validate_dag_spec(dag.to_dag_spec())


def test_branch_edge_requires_condition_source() -> None:
    dag = dagent.Dag("invalid_branch_source", input=str)
    first = dagent.Node("first", target=publish, inputs={"content": dag.input})
    second = dagent.Node("second", target=publish, inputs={"content": dag.input})
    dag.add_node(first)
    dag.add_node(second)
    dag.add_edge(first, second, branch="go")

    with pytest.raises(DAGValidationError, match="source is not a condition node"):
        validate_dag_spec(dag.to_dag_spec())


def test_condition_branches_are_unique_and_default_is_distinct() -> None:
    duplicate = dagent.Dag("duplicate_condition", input=bool)
    duplicate.add_node(
        dagent.ConditionNode(
            "route",
            cases=[dagent.Case("same", duplicate.input), dagent.Case("same", duplicate.input)],
            default_branch="other",
        )
    )
    with pytest.raises(DAGValidationError, match="duplicate case branches: same"):
        validate_dag_spec(duplicate.to_dag_spec())

    conflicting = dagent.Dag("conflicting_condition", input=bool)
    conflicting.add_node(
        dagent.ConditionNode(
            "route",
            cases=[dagent.Case("same", conflicting.input)],
            default_branch="same",
        )
    )
    with pytest.raises(DAGValidationError, match="duplicates a case branch"):
        validate_dag_spec(conflicting.to_dag_spec())


@dagent.tool
def fetch(url: str) -> str:
    """Fetch a url."""

    return f"page:{url}"


def test_map_node_fans_out_over_runtime_list() -> None:
    @dagent.tool
    def collect(pages: list) -> str:
        return ",".join(pages)

    dag = dagent.Dag("fanout", input=list)
    fetch_all = dagent.MapNode(
        "fetch_all",
        target=fetch,
        over=dag.input,
        inputs={"url": dagent.item},
    )
    collect_node = dagent.Node("collect", target=collect, inputs={"pages": fetch_all.output})
    dag.add_node(fetch_all)
    dag.add_node(collect_node)
    dag.add_edge(fetch_all, collect_node)

    result = run_dag(dag, ["a", "b", "c"], [fetch, collect])

    assert result.status == "completed"
    assert result.node_value("fetch_all") == ["page:a", "page:b", "page:c"]
    assert result.node_value("collect") == "page:a,page:b,page:c"
    map_trace = result.trace.dag_node_traces()["fetch_all"]
    assert len(map_trace.children) == 3


def test_map_node_item_path_access() -> None:
    dag = dagent.Dag("fanout_path", input=list)
    fetch_all = dagent.MapNode(
        "fetch_all",
        target=fetch,
        over=dag.input,
        inputs={"url": dagent.item["url"]},
    )
    dag.add_node(fetch_all)

    result = run_dag(dag, [{"url": "x"}, {"url": "y"}], [fetch])

    assert result.node_value("fetch_all") == ["page:x", "page:y"]


def test_map_node_fails_closed_above_max_items() -> None:
    dag = dagent.Dag("fanout_limit", input=list)
    fetch_all = dagent.MapNode(
        "fetch_all",
        target=fetch,
        over=dag.input,
        inputs={"url": dagent.item},
        max_items=2,
    )
    dag.add_node(fetch_all)

    result = run_dag(dag, ["a", "b", "c"], [fetch])

    assert result.status == "failed"


def test_item_expression_rejected_outside_map() -> None:
    dag = dagent.Dag("bad_item", input=str)
    node = dagent.Node("publish", target=publish, inputs={"content": dagent.item})
    dag.add_node(node)

    with pytest.raises(DAGValidationError, match="item expression"):
        validate_dag_spec(dag.to_dag_spec())


def _report_subgraph() -> dagent.Dag:
    sub = dagent.Dag("report", input=str)
    fetch_node = dagent.Node("fetch", target=fetch, inputs={"url": sub.input})
    publish_node = dagent.Node("publish", target=publish, inputs={"content": fetch_node.output})
    sub.add_node(fetch_node)
    sub.add_node(publish_node)
    sub.add_edge(fetch_node, publish_node)
    sub.output = publish_node.output
    return sub


def test_subgraph_node_runs_embedded_dag_and_returns_declared_output() -> None:
    dag = dagent.Dag("outer", input=str)
    report_node = dagent.Node("make_report", target=_report_subgraph(), inputs=dag.input)
    dag.add_node(report_node)

    result = run_dag(dag, "example.test", [])

    assert result.status == "completed"
    assert result.node_value("make_report") == "published:page:example.test"
    sub_trace = result.trace.dag_node_traces()["make_report"]
    assert sub_trace.children[0].kind == "run"
    assert {child.ref.get("node_id") for child in sub_trace.children[0].children} == {"fetch", "publish"}


def test_subgraph_absorbs_child_capabilities_into_parent() -> None:
    dag = dagent.Dag("outer", input=str)
    dag.add_node(dagent.Node("make_report", target=_report_subgraph(), inputs=dag.input))

    assert {binding.definition.id for binding in dag.capabilities} == {"tool.fetch", "tool.publish"}


def test_subgraph_input_can_reference_parent_nodes() -> None:
    dag = dagent.Dag("outer", input=str)
    fetch_node = dagent.Node("locate", target=fetch, inputs={"url": dag.input})
    report_node = dagent.Node("make_report", target=_report_subgraph(), inputs=fetch_node.output)
    dag.add_node(fetch_node)
    dag.add_node(report_node)
    dag.add_edge(fetch_node, report_node)

    result = run_dag(dag, "root", [fetch])

    assert result.node_value("make_report") == "published:page:page:root"


def test_subgraph_rejects_invalid_resolved_input_before_capability_execution() -> None:
    calls: list[str | int] = []

    @dagent.tool
    def accept_nested_input(value: str | int) -> str:
        calls.append(value)
        return str(value)

    child = dagent.Dag("integer_child", input=int)
    child_node = dagent.Node(
        "accept",
        target=accept_nested_input,
        inputs={"value": child.input},
    )
    child.add_node(child_node)
    child.output = child_node.output

    outer = dagent.Dag("outer_string", input=str)
    outer.add_node(dagent.Node("child", target=child, inputs=outer.input))

    result = run_dag(outer, "not-an-integer", [])

    assert result.status == "failed"
    assert calls == []
    assert result.trace.root.error is not None
    assert result.trace.root.error.code == "DAGInputValidationError"


def test_spec_output_must_reference_known_node() -> None:
    dag = dagent.Dag("bad_output", input=str)
    dag.add_node(dagent.Node("publish", target=publish, inputs={"content": dag.input}))
    dag.output = dagent.NodeOutputRef("missing")

    with pytest.raises(DAGValidationError, match="unknown node 'missing'"):
        validate_dag_spec(dag.to_dag_spec())


@dagent.tool
def increment(n: int) -> int:
    """Add one."""

    return n + 1


def _increment_body() -> dagent.Dag:
    body = dagent.Dag("step", input=int)
    inc = dagent.Node("inc", target=increment, inputs={"n": body.input})
    body.add_node(inc)
    body.output = inc.output
    return body


def test_loop_node_iterates_until_condition() -> None:
    dag = dagent.Dag("refine", input=int)
    loop = dagent.LoopNode(
        "count_up",
        body=_increment_body(),
        until=dagent.item >= 3,
        max_iterations=10,
        input=dag.input,
    )
    dag.add_node(loop)

    result = run_dag(dag, 0, [])

    assert result.status == "completed"
    assert result.node_value("count_up") == 3
    loop_trace = result.trace.dag_node_traces()["count_up"]
    assert len(loop_trace.children) == 3


def test_loop_node_stops_at_max_iterations_with_last_value() -> None:
    dag = dagent.Dag("bounded", input=int)
    loop = dagent.LoopNode(
        "count_up",
        body=_increment_body(),
        until=dagent.item >= 100,
        max_iterations=4,
        input=dag.input,
    )
    dag.add_node(loop)

    result = run_dag(dag, 0, [])

    assert result.status == "completed"
    assert result.node_value("count_up") == 4


def test_loop_validates_each_iteration_input_before_capability_execution() -> None:
    calls: list[int] = []

    @dagent.tool
    def stringify_iteration(value: int) -> str:
        calls.append(value)
        return str(value)

    body = dagent.Dag("integer_to_string", input=int)
    stringify = dagent.Node(
        "stringify",
        target=stringify_iteration,
        inputs={"value": body.input},
    )
    body.add_node(stringify)
    body.output = stringify.output

    dag = dagent.Dag("invalid_second_iteration", input=int)
    loop = dagent.LoopNode(
        "repeat",
        body=body,
        until=dagent.item == "stop",
        max_iterations=2,
        input=dag.input,
    )
    dag.add_node(loop)

    result = run_dag(dag, 1, [])

    assert result.status == "failed"
    assert calls == [1]
    assert result.trace.root.error is not None
    assert result.trace.root.error.code == "DAGInputValidationError"
    assert len(result.trace.dag_node_traces()["repeat"].children) == 1


def test_loop_requires_positive_max_iterations() -> None:
    with pytest.raises(Exception):
        dag = dagent.Dag("invalid_loop", input=int)
        dag.add_node(
            dagent.LoopNode(
                "loop",
                body=_increment_body(),
                until=dagent.item >= 1,
                max_iterations=0,
                input=dag.input,
            )
        )


def test_nested_specs_are_validated_recursively() -> None:
    bad_child = dagent.Dag("child", input=str)
    a = dagent.Node("a", target=publish, inputs={"content": bad_child.input})
    b = dagent.Node("b", target=publish, inputs={"content": a.output})
    bad_child.add_node(a)
    bad_child.add_node(b)  # b reads a.output but has no edge

    dag = dagent.Dag("outer", input=str)
    dag.add_node(dagent.Node("sub", target=bad_child, inputs=dag.input))

    with pytest.raises(DAGValidationError, match="embedded DAG"):
        validate_dag_spec(dag.to_dag_spec())


def test_review_gate_sees_risk_inside_subgraph() -> None:
    @dagent.tool(risk="high")
    def deploy(target: str) -> str:
        return f"deployed:{target}"

    child = dagent.Dag("child", input=str)
    child.add_node(dagent.Node("deploy", target=deploy, inputs={"target": child.input}))

    dag = dagent.Dag("outer", input=str)
    dag.add_node(dagent.Node("sub", target=child, inputs=dag.input))

    from dagent.schemas import iter_dag_invocations

    risks = [invocation.risk for invocation in iter_dag_invocations(dag.to_dag_spec().nodes)]
    assert risks == ["high"]


def test_conditional_edge_round_trips_through_spec_json() -> None:
    dag = _branching_dag()
    spec = dag.to_dag_spec()
    restored = type(spec).model_validate(spec.model_dump(mode="json"))

    when = restored.edges[0].when
    assert when is not None
    assert when.expr.op == "ge"


def test_static_dag_output_becomes_output_text() -> None:
    dag = dagent.Dag("with_output", input=str)
    node = dagent.Node("publish", target=publish, inputs={"content": dag.input})
    dag.add_node(node)
    dag.output = node.output

    result = run_dag(dag, "x", [publish])

    assert result.output_text == "published:x"
    assert result.trace.root.value == "published:x"


def test_static_dag_structured_output_serializes_to_json_text() -> None:
    dag = dagent.Dag("with_dict_output", input=str)
    node = dagent.Node("score", target=score, inputs={"text": dag.input})
    dag.add_node(node)
    dag.output = node.output

    result = run_dag(dag, "good text", [score])

    assert result.trace.root.value == {"score": 0.9}
    assert result.output_text == '{"score": 0.9}'


def test_map_node_accepts_literal_list() -> None:
    dag = dagent.Dag("literal_fanout", input=str)
    fetch_all = dagent.MapNode(
        "fetch_all",
        target=fetch,
        over=["a", "b"],
        inputs={"url": dagent.item},
    )
    dag.add_node(fetch_all)

    result = run_dag(dag, "unused", [fetch])

    assert result.node_value("fetch_all") == ["page:a", "page:b"]


def test_map_over_agent_isolates_item_sessions_and_keeps_inner_traces() -> None:
    provider = MockProvider([ChatResponse(content="r1"), ChatResponse(content="r2")])
    runner = dagent.Runner(
        workspace=".",
        runtime_directory=".runtime",
        provider=provider,
    )
    agent = dagent.ToolAgent(profile="conversation", name="writer", max_steps=1)

    dag = dagent.Dag("agent_map", input=list)
    map_node = dagent.MapNode(
        "write_all",
        target=agent,
        over=dag.input,
        inputs={"prompt": dagent.item},
        max_concurrency=1,
    )
    dag.add_node(map_node)

    try:
        result = run(runner.run(dag, graph_input=["alpha", "beta"]))
    finally:
        runner.close()

    assert result.status == "completed"
    assert result.node_value("write_all") == ["r1", "r2"]
    user_texts = [
        "\n".join(str(m.get("content")) for m in request["messages"] if m.get("role") == "user")
        for request in provider.requests
    ]
    assert "alpha" in user_texts[0] and "alpha" not in user_texts[1]
    assert "beta" in user_texts[1]
    item_calls = result.trace.dag_node_traces()["write_all"].children
    assert all(
        any(grandchild.kind == "agent_loop" for grandchild in child.children)
        for child in item_calls
    )


def test_edge_condition_rejects_unknown_artifact() -> None:
    dag = dagent.Dag("when_artifact", input=str)
    a = dagent.Node("a", target=publish, inputs={"content": dag.input})
    b = dagent.Node("b", target=publish, inputs={"content": dag.input})
    dag.add_node(a)
    dag.add_node(b)
    dag.add_edge(a, b, when=dagent.ArtifactValueRef("ghost"))

    with pytest.raises(DAGValidationError, match="unknown artifact 'ghost'"):
        validate_dag_spec(dag.to_dag_spec())


def test_dag_output_rejects_unknown_artifact() -> None:
    dag = dagent.Dag("output_artifact", input=str)
    dag.add_node(dagent.Node("publish", target=publish, inputs={"content": dag.input}))
    dag.output = dagent.ArtifactValueRef("ghost")

    with pytest.raises(DAGValidationError, match="unknown artifact 'ghost'"):
        validate_dag_spec(dag.to_dag_spec())


def test_loop_until_participates_in_artifact_inference() -> None:
    dag = dagent.Dag("loop_artifacts", input=int)
    report = dag.artifact("report", "outputs/report.md")
    loop = dagent.LoopNode(
        "count_up",
        body=_increment_body(),
        until=dagent.item == report.path,
        max_iterations=2,
        input=dag.input,
    )
    dag.add_node(loop)

    assert dag.to_dag_spec().nodes[0].inputs == ["report"]


def test_embedded_run_trace_ids_are_unique_across_iterations() -> None:
    dag = dagent.Dag("unique_ids", input=int)
    loop = dagent.LoopNode(
        "count_up",
        body=_increment_body(),
        until=dagent.item >= 2,
        max_iterations=5,
        input=dag.input,
    )
    dag.add_node(loop)

    result = run_dag(dag, 0, [])

    loop_trace = result.trace.dag_node_traces()["count_up"]
    iteration_ids = [child.id for child in loop_trace.children]
    assert len(iteration_ids) == 2
    assert len(set(iteration_ids)) == 2
    assert result.trace.root.id not in iteration_ids
    for child in loop_trace.children:
        assert all(grandchild.parent_id == child.id for grandchild in child.children)


def test_compare_expr_requires_both_operands() -> None:
    from dagent.schemas.value import CompareExpr

    with pytest.raises(ValidationError):
        CompareExpr(type="compare", op="eq", left=1)


def test_list_files_value_feeds_map_fanout(tmp_path) -> None:
    runner = dagent.Runner(
        workspace=tmp_path,
        runtime_directory=".runtime",
        provider=MockProvider([]),
    )

    dag = dagent.Dag("list_then_map", input=str)
    write_a = dagent.Node(
        "write_a",
        target="tool.write_file",
        inputs={"path": "notes/a.md", "content": "alpha"},
        boundary=dagent.Boundary(allowed_paths=["."]),
    )
    write_b = dagent.Node(
        "write_b",
        target="tool.write_file",
        inputs={"path": "notes/b.md", "content": "beta"},
        boundary=dagent.Boundary(allowed_paths=["."]),
    )
    listing = dagent.Node(
        "listing",
        target="tool.list_files",
        inputs={"path": ".", "glob": "*.md"},
    )
    read_all = dagent.MapNode(
        "read_all",
        target="tool.read_file",
        over=listing.output,
        inputs={"path": dagent.item},
    )
    dag.add_node(write_a)
    dag.add_node(write_b)
    dag.add_node(listing)
    dag.add_node(read_all)
    dag.add_edge(write_a, listing)
    dag.add_edge(write_b, listing)
    dag.add_edge(listing, read_all)

    try:
        result = run(
            runner.run(dag, graph_input="unused", workspace_root=tmp_path / "runs")
        )
    finally:
        runner.close()

    assert result.status == "completed"
    assert len(result.node_value("listing")) == 2
    assert result.node_value("read_all") == ["alpha", "beta"]
