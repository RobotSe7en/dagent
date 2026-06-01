import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

import dagent
from dagent.providers import ChatResponse, MockProvider
from dagent.schemas import DAGSpec
from dagent.harness_runtime.dag_builder import DAGValidationError


def run(coro):
    return asyncio.run(coro)


class SearchResult(BaseModel):
    title: str
    url: str


class QueryInput(BaseModel):
    query: str


def test_dag_builder_creates_capability_nodes_edges_and_refs() -> None:
    @dagent.tool
    def search(q: str) -> SearchResult:
        return SearchResult(title=f"found:{q}", url="https://example.test")

    @dagent.tool(risk="medium")
    def write_file(path: str, title: str, url: str) -> str:
        return f"wrote:{path}:{title}:{url}"

    dag = dagent.Dag("research_report", name="Research Report", input=str)
    report = dag.artifact("report", "outputs/report.md")
    search_node = dag.capability_node("search", search, q=dag.input)
    write_node = dag.capability_node(
        "write_report",
        write_file,
        path=report.path,
        title=search_node.output["title"],
        url=search_node.output["url"],
        outputs=[report],
    ).after(search_node)

    spec = dag.to_dag_spec()

    assert isinstance(spec, DAGSpec)
    assert spec.id == "research_report"
    assert spec.name == "Research Report"
    assert spec.input_schema == {"type": "string"}
    assert spec.artifacts["report"].paths == ["outputs/report.md"]
    assert [node.id for node in spec.nodes] == ["search", "write_report"]
    assert spec.nodes[0].payload.invocation.capability_id == "tool.search"
    assert search.definition.output_schema["properties"]["title"]["type"] == "string"
    assert spec.nodes[0].payload.invocation.arguments == {
        "q": {"$expr": {"type": "graph_input", "path": []}},
    }
    assert spec.nodes[1].payload.invocation.arguments == {
        "path": {"$expr": {"type": "artifact", "artifact_id": "report", "field": "path"}},
        "title": {
            "$expr": {
                "type": "node_output",
                "node_id": "search",
                "field": "value",
                "path": ["title"],
            }
        },
        "url": {
            "$expr": {
                "type": "node_output",
                "node_id": "search",
                "field": "value",
                "path": ["url"],
            }
        },
    }
    assert spec.nodes[1].outputs == ["report"]
    assert [(edge.source, edge.target) for edge in spec.edges] == [("search", "write_report")]
    assert write_node.id == "write_report"


def test_value_refs_support_common_path_field_names() -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return text

    dag = dagent.Dag("path_refs")
    source = dag.capability_node("source", echo, text="hello")

    assert dag.input.path.as_expr() == {
        "$expr": {"type": "graph_input", "path": ["path"]},
    }
    assert source.output.path.as_expr() == {
        "$expr": {"type": "node_output", "node_id": "source", "field": "value", "path": ["path"]},
    }


def test_dag_builder_supports_fan_out_and_fan_in() -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return text

    dag = dagent.Dag("fan")
    root = dag.capability_node("root", echo, text="start")
    a = dag.capability_node("a", echo, text=root.output).after(root)
    b = dag.capability_node("b", echo, text=root.output).after(root)
    c = dag.capability_node("c", echo, text=root.output).after(root)
    dag.capability_node("join", echo, text=a.output).after(a, b, c)

    spec = dag.to_dag_spec()

    assert sorted((edge.source, edge.target) for edge in spec.edges) == [
        ("a", "join"),
        ("b", "join"),
        ("c", "join"),
        ("root", "a"),
        ("root", "b"),
        ("root", "c"),
    ]


def test_dag_builder_rejects_duplicate_nodes_and_unknown_edges() -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return text

    dag = dagent.Dag("bad")
    dag.capability_node("echo", echo, text="one")

    with pytest.raises(ValueError, match="already exists"):
        dag.capability_node("echo", echo, text="two")

    with pytest.raises(ValueError, match="Unknown node"):
        dag.edge("missing", "echo")


def test_dag_builder_requires_explicit_edge_for_node_output_ref() -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return text

    dag = dagent.Dag("missing_edge")
    source = dag.capability_node("source", echo, text="hello")
    dag.capability_node("sink", echo, text=source.output)

    with pytest.raises(DAGValidationError, match="must depend"):
        dagent.validate_dag_spec(dag.to_dag_spec())


def test_runner_executes_builder_with_collected_capabilities(tmp_path: Path) -> None:
    @dagent.tool(supports_context=True)
    def write_note(path: str, content: str, *, context, callbacks=None) -> str:
        resolved = Path(context.workspace_path) / path
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"wrote:{path}"

    dag = dagent.Dag("write_note")
    note = dag.artifact("note", "notes/output.txt")
    dag.capability_node(
        "write",
        write_note,
        path="notes/output.txt",
        content="hi",
        outputs=[note],
    )

    runner = dagent.Runner(workspace=tmp_path, provider=MockProvider([]))
    result = run(runner.run(dag, workspace_root=tmp_path / "runs"))

    assert result.status == "completed"
    assert result.trace.artifacts["note"].status == "created"
    workspace = Path(result.workspace_path)
    assert (workspace / "notes" / "output.txt").read_text(encoding="utf-8") == "hi"


def test_runner_executes_value_expr_dataflow(tmp_path: Path) -> None:
    @dagent.tool
    def search(q: str) -> SearchResult:
        return SearchResult(title=f"found:{q}", url="https://example.test")

    @dagent.tool
    def render(title: str, url: str) -> str:
        return f"{title} <{url}>"

    dag = dagent.Dag("research", input=str)
    search_node = dag.capability_node("search", search, q=dag.input)
    dag.capability_node(
        "render",
        render,
        title=search_node.output["title"],
        url=search_node.output["url"],
    ).after(search_node)

    runner = dagent.Runner(workspace=tmp_path, provider=MockProvider([]))
    result = run(runner.run(dag, input="dagent", workspace_root=tmp_path / "runs"))

    assert result.status == "completed"
    assert result.trace.root.children[0].value == {"title": "found:dagent", "url": "https://example.test"}
    assert result.trace.root.children[1].output == "found:dagent <https://example.test>"


def test_runner_resolves_pydantic_graph_input(tmp_path: Path) -> None:
    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    dag = dagent.Dag("research", input=QueryInput)
    dag.capability_node("search", search, q=dag.input.query)

    runner = dagent.Runner(workspace=tmp_path, provider=MockProvider([]))
    result = run(runner.run(
        dag,
        input=QueryInput(query="dagent"),
        workspace_root=tmp_path / "runs",
    ))

    assert result.status == "completed"
    assert result.trace.root.children[0].output == "found:dagent"


def test_agent_node_generates_agent_capability_invocation(tmp_path: Path) -> None:
    provider = MockProvider([ChatResponse(content="drafted")])
    writer = dagent.ToolAgent(
        profile=_profile_root(tmp_path, "writer"),
    )
    dag = dagent.Dag("agent_flow")
    draft = dag.agent_node("draft", writer, prompt="Draft the report.", max_steps=3)

    spec = dag.to_dag_spec()

    invocation = spec.nodes[0].payload.invocation
    assert draft.output.as_expr() == {"$expr": {"type": "node_output", "node_id": "draft", "field": "value", "path": []}}
    assert invocation.capability_id == "agent.writer"
    assert invocation.kind == "agent"
    assert invocation.arguments == {"prompt": "Draft the report.", "max_steps": 3}

    runner = dagent.Runner(workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")
    result = run(runner.run(dag, workspace_root=tmp_path / "runs"))
    assert result.status == "completed"


def test_agent_node_prompt_accepts_value_expr_from_previous_node(tmp_path: Path) -> None:
    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    provider = MockProvider([ChatResponse(content="drafted")])
    writer = dagent.ToolAgent(profile=_profile_root(tmp_path, "writer"))

    dag = dagent.Dag("agent_flow", input=str)
    search_node = dag.capability_node("search", search, q=dag.input)
    draft = dag.agent_node(
        "draft",
        writer,
        prompt=dag.format("Draft from {result}", result=search_node.output),
    ).after(search_node)

    runner = dagent.Runner(workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")
    result = run(runner.run(dag, input="dagent", workspace_root=tmp_path / "runs"))

    assert result.status == "completed"
    assert draft.output.as_expr() == {"$expr": {"type": "node_output", "node_id": "draft", "field": "value", "path": []}}
    assert "Draft from found:dagent" in provider.requests[0]["messages"][1]["content"]


def test_agent_node_defaults_to_tool_agent_max_steps(tmp_path: Path) -> None:
    writer = dagent.ToolAgent(
        profile=_profile_root(tmp_path, "writer"),
        max_steps=11,
    )
    dag = dagent.Dag("agent_flow")

    dag.agent_node("draft", writer, prompt="Draft the report.")

    invocation = dag.to_dag_spec().nodes[0].payload.invocation
    assert invocation.arguments == {"prompt": "Draft the report.", "max_steps": 11}


def _profile_root(tmp_path: Path, name: str) -> str:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_path = profile_dir / f"{name}.md"
    profile_path.write_text(f"# {name}\n\nYou are {name}.", encoding="utf-8")
    return name
