"""Static DAG control flow: conditional edges, map fan-out, subgraphs, and loops."""

import asyncio

import pytest

import dagent
from dagent.harness_runtime.dag_builder import DAGValidationError, validate_dag_spec
from dagent.providers import MockProvider


def run(coro):
    return asyncio.run(coro)


def run_dag(dag, graph_input, capabilities):
    runner = dagent.Runner(provider=MockProvider([]), capabilities=capabilities)
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


def test_map_item_expression_rejected_outside_map() -> None:
    dag = dagent.Dag("bad_item", input=str)
    node = dagent.Node("publish", target=publish, inputs={"content": dagent.item})
    dag.add_node(node)

    with pytest.raises(DAGValidationError, match="map_item"):
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
