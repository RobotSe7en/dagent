import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall
from dagent.schemas import DAGSpec
from dagent.harness_runtime.dag_builder import DAGValidationError


def run(coro):
    return asyncio.run(coro)


class SearchResult(BaseModel):
    title: str
    url: str


class QueryInput(BaseModel):
    query: str


def test_dag_builder_exposes_single_node_api() -> None:
    assert not hasattr(dagent.Dag, "capability_node")
    assert not hasattr(dagent.Dag, "agent_node")
    assert not hasattr(dagent, "NodeRef")


def test_dag_builder_adds_user_nodes_edges_and_artifact_contracts() -> None:
    @dagent.tool
    def search(q: str) -> SearchResult:
        return SearchResult(title=f"found:{q}", url="https://example.test")

    dag = dagent.Dag("research_report", name="Research Report", input=QueryInput)
    source = dag.artifact("source", "inputs/source.md")
    report = dag.artifact("report", "outputs/report.md")

    research = dagent.Node(
        "research",
        target=search,
        inputs={"q": dag.input.query},
        artifact_inputs=[source],
    )
    write = dagent.Node(
        "write",
        target="tool.write_file",
        inputs={
            "path": report.path,
            "content": research.output["title"],
        },
        artifact_outputs=[report],
    )
    review = dagent.Node(
        "review",
        target="tool.read_file",
        inputs={"path": report.path},
    )

    dag.add_node(research)
    dag.add_node(write)
    dag.add_node(review)
    dag.add_edge(research, write)
    dag.add_edge(write, review)

    spec = dag.to_dag_spec()

    assert [node.id for node in spec.nodes] == ["research", "write", "review"]
    assert spec.nodes[0].payload.invocation.capability_id == "tool.search"
    assert spec.nodes[0].payload.invocation.arguments == {
        "q": {"$expr": {"type": "graph_input", "path": ["query"]}},
    }
    assert spec.nodes[0].inputs == ["source"]
    assert spec.nodes[1].payload.invocation.arguments == {
        "path": {"$expr": {"type": "artifact", "artifact_id": "report", "field": "path"}},
        "content": {
            "$expr": {
                "type": "node_output",
                "node_id": "research",
                "field": "value",
                "path": ["title"],
            }
        },
    }
    assert spec.nodes[1].inputs == []
    assert spec.nodes[1].outputs == ["report"]
    assert spec.nodes[2].inputs == ["report"]
    assert spec.nodes[2].outputs == []
    assert [(edge.source, edge.target) for edge in spec.edges] == [
        ("research", "write"),
        ("write", "review"),
    ]


def test_dag_builder_user_node_can_target_tool_agent() -> None:
    writer = dagent.ToolAgent(profile="conversation", name="writer", max_steps=3)
    dag = dagent.Dag("agent_node")

    draft = dagent.Node("draft", target=writer, inputs={"prompt": "Draft the report."})
    dag.add_node(draft)

    spec = dag.to_dag_spec()

    assert spec.nodes[0].payload.invocation.capability_id == "agent.writer"
    assert spec.nodes[0].payload.invocation.kind == "agent"
    assert spec.nodes[0].payload.invocation.risk == "medium"
    assert spec.nodes[0].payload.invocation.arguments == {"prompt": "Draft the report."}
    assert draft.output.as_expr() == {
        "$expr": {"type": "node_output", "node_id": "draft", "field": "value", "path": []}
    }


def test_dag_builder_user_node_infers_artifact_inputs_from_boundary() -> None:
    dag = dagent.Dag("read_source")
    source = dag.artifact("source", "inputs/source.md")

    node = dagent.Node(
        "read_source",
        target="tool.read_file",
        inputs={"path": "inputs/source.md"},
        boundary=dagent.Boundary(
            mode="read_only",
            allowed_paths=[source.path.as_expr()],
        ),
    )
    dag.add_node(node)

    spec = dag.to_dag_spec()

    assert spec.nodes[0].inputs == ["source"]


def test_runner_executes_user_node_value_dataflow() -> None:
    @dagent.tool
    def source(text: str) -> dict[str, str]:
        return {"summary": f"summary:{text}"}

    @dagent.tool
    def sink(content: str) -> str:
        return f"sink:{content}"

    dag = dagent.Dag("node_dataflow", input=str)
    source_node = dagent.Node("source", target=source, inputs={"text": dag.input})
    sink_node = dagent.Node("sink", target=sink, inputs={"content": source_node.output["summary"]})
    dag.add_node(source_node)
    dag.add_node(sink_node)
    dag.add_edge(source_node, sink_node)

    result = run(dagent.Runner(provider=MockProvider([]), capabilities=[source, sink]).run(
        dag,
        graph_input="hello",
    ))

    assert result.status == "completed"
    assert result.node_value("sink") == "sink:summary:hello"


def test_dag_builder_creates_capability_nodes_edges_and_refs() -> None:
    @dagent.tool
    def search(q: str) -> SearchResult:
        return SearchResult(title=f"found:{q}", url="https://example.test")

    @dagent.tool(risk="medium")
    def write_file(path: str, title: str, url: str) -> str:
        return f"wrote:{path}:{title}:{url}"

    dag = dagent.Dag("research_report", name="Research Report", input=str)
    report = dag.artifact("report", "outputs/report.md")
    search_node = dagent.Node("search", target=search, inputs={"q": dag.input})
    write_node = dagent.Node(
        "write_report",
        target=write_file,
        inputs={
            "path": report.path,
            "title": search_node.output["title"],
            "url": search_node.output["url"],
        },
        artifact_outputs=[report],
    )
    dag.add_node(search_node)
    dag.add_node(write_node)
    dag.add_edge(search_node, write_node)

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
    source = dagent.Node("source", target=echo, inputs={"text": "hello"})
    dag.add_node(source)

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
    root = dagent.Node("root", target=echo, inputs={"text": "start"})
    a = dagent.Node("a", target=echo, inputs={"text": root.output})
    b = dagent.Node("b", target=echo, inputs={"text": root.output})
    c = dagent.Node("c", target=echo, inputs={"text": root.output})
    join = dagent.Node("join", target=echo, inputs={"text": a.output})
    for node in (root, a, b, c, join):
        dag.add_node(node)
    for node in (a, b, c):
        dag.add_edge(root, node)
    for node in (a, b, c):
        dag.add_edge(node, join)

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
    dag.add_node(dagent.Node("echo", target=echo, inputs={"text": "one"}))

    with pytest.raises(ValueError, match="already exists"):
        dag.add_node(dagent.Node("echo", target=echo, inputs={"text": "two"}))

    with pytest.raises(ValueError, match="Unknown node"):
        dag.add_edge("missing", "echo")


def test_dag_builder_requires_explicit_edge_for_node_output_ref() -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return text

    dag = dagent.Dag("missing_edge")
    source = dagent.Node("source", target=echo, inputs={"text": "hello"})
    sink = dagent.Node("sink", target=echo, inputs={"text": source.output})
    dag.add_node(source)
    dag.add_node(sink)

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
    dag.add_node(dagent.Node(
        "write",
        target=write_note,
        inputs={"path": "notes/output.txt", "content": "hi"},
        artifact_outputs=[note],
    ))

    runner = dagent.Runner(workspace=tmp_path, provider=MockProvider([]))
    result = run(runner.run(dag, workspace_root=tmp_path / "runs"))

    assert isinstance(result, dagent.RunResult)
    assert result.kind == "static_dag"
    assert result.dag_run is not None
    assert result.run_id == result.dag_run.run_id
    assert result.spec_id == "write_note"
    assert result.status == "completed"
    assert result.artifact_state("note").status == "created"
    assert result.artifacts["note"].status == "created"
    assert result.node_output("write") == "wrote:notes/output.txt"
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
    search_node = dagent.Node("search", target=search, inputs={"q": dag.input})
    render_node = dagent.Node(
        "render",
        target=render,
        inputs={
            "title": search_node.output["title"],
            "url": search_node.output["url"],
        },
    )
    dag.add_node(search_node)
    dag.add_node(render_node)
    dag.add_edge(search_node, render_node)

    runner = dagent.Runner(workspace=tmp_path, provider=MockProvider([]))
    result = run(runner.run(dag, graph_input="dagent", workspace_root=tmp_path / "runs"))

    assert isinstance(result, dagent.RunResult)
    assert result.status == "completed"
    assert result.node_value("search") == {"title": "found:dagent", "url": "https://example.test"}
    assert result.node_output("render") == "found:dagent <https://example.test>"


def test_runner_resolves_pydantic_graph_input(tmp_path: Path) -> None:
    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    dag = dagent.Dag("research", input=QueryInput)
    dag.add_node(dagent.Node("search", target=search, inputs={"q": dag.input.query}))

    runner = dagent.Runner(workspace=tmp_path, provider=MockProvider([]))
    result = run(runner.run(
        dag,
        graph_input=QueryInput(query="dagent"),
        workspace_root=tmp_path / "runs",
    ))

    assert result.status == "completed"
    assert result.trace.root.children[0].output == "found:dagent"


def test_runner_stream_static_dag_done_result_is_unified_run_result(tmp_path: Path) -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return f"echo:{text}"

    dag = dagent.Dag("echo_dag", input=str)
    dag.add_node(dagent.Node("echo", target=echo, inputs={"text": dag.input}))
    runner = dagent.Runner(workspace=tmp_path, provider=MockProvider([]))

    async def collect() -> list[dagent.RunStreamEvent]:
        return [
            event
            async for event in runner.stream_events(dag, graph_input="hello", workspace_root=tmp_path / "runs")
        ]

    events = run(collect())

    trace_events = [event for event in events if event.type == "trace.updated"]
    assert trace_events
    assert trace_events[-1].data.trace.status == "completed"
    assert events[-1].type == "run.finished"
    assert isinstance(events[-1].data.result, dagent.RunResult)
    assert events[-1].data.result.kind == "static_dag"
    assert events[-1].data.result.node_output("echo") == "echo:hello"


def test_runner_stream_static_dag_capability_events_keep_node_context(tmp_path: Path) -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return f"echo:{text}"

    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})]),
        ChatResponse(content="done"),
    ])
    writer = dagent.ToolAgent(
        profile=_profile_root(tmp_path, "writer"),
        capabilities=[echo],
    )
    dag = dagent.Dag("agent_tool_events")
    dag.add_node(dagent.Node("draft", target=writer, inputs={"prompt": "Use echo."}))
    runner = dagent.Runner(workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    async def collect() -> list[dagent.RunStreamEvent]:
        return [event async for event in runner.stream_events(dag, workspace_root=tmp_path / "runs")]

    events = run(collect())

    started = next(event for event in events if event.type == "capability.call.started")
    completed = next(event for event in events if event.type == "capability.call.completed")
    assert started.data.invocation_id == "call_1"
    assert started.data.capability_id == "tool.echo"
    assert started.data.arguments == {"text": "hi"}
    assert started.data.run_id is not None
    assert started.data.dag_id is not None
    assert started.data.node_id == "draft"
    assert started.data.parent_capability_id == "agent.writer"
    assert completed.data.run_id == started.data.run_id
    assert completed.data.dag_id == started.data.dag_id
    assert completed.data.node_id == "draft"
    assert completed.data.parent_capability_id == "agent.writer"


def test_runner_runs_dag_spec_with_unified_run_result(tmp_path: Path) -> None:
    @dagent.tool
    def echo(text: str) -> str:
        return f"echo:{text}"

    dag = dagent.Dag("echo_spec", input=str)
    dag.add_node(dagent.Node("echo", target=echo, inputs={"text": dag.input}))
    runner = dagent.Runner(workspace=tmp_path, provider=MockProvider([]), capabilities=[echo])

    result = run(runner.run(dag.to_dag_spec(), graph_input="hello", workspace_root=tmp_path / "runs"))

    assert isinstance(result, dagent.RunResult)
    assert result.kind == "static_dag"
    assert result.spec_id == "echo_spec"
    assert result.node_output("echo") == "echo:hello"


def test_agent_node_generates_agent_capability_invocation(tmp_path: Path) -> None:
    provider = MockProvider([ChatResponse(content="drafted")])
    writer = dagent.ToolAgent(
        profile=_profile_root(tmp_path, "writer"),
    )
    dag = dagent.Dag("agent_flow")
    draft = dagent.Node(
        "draft",
        target=writer,
        inputs={"prompt": "Draft the report.", "max_steps": 3},
    )
    dag.add_node(draft)

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
    search_node = dagent.Node("search", target=search, inputs={"q": dag.input})
    draft = dagent.Node(
        "draft",
        target=writer,
        inputs={"prompt": dag.format("Draft from {result}", result=search_node.output)},
    )
    dag.add_node(search_node)
    dag.add_node(draft)
    dag.add_edge(search_node, draft)

    runner = dagent.Runner(workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")
    result = run(runner.run(dag, graph_input="dagent", workspace_root=tmp_path / "runs"))

    assert result.status == "completed"
    assert draft.output.as_expr() == {"$expr": {"type": "node_output", "node_id": "draft", "field": "value", "path": []}}
    assert "Draft from found:dagent" in provider.requests[0]["messages"][1]["content"]


def test_agent_node_keeps_tool_agent_config_out_of_node_inputs(tmp_path: Path) -> None:
    writer = dagent.ToolAgent(
        profile=_profile_root(tmp_path, "writer"),
        max_steps=11,
    )
    dag = dagent.Dag("agent_flow")

    dag.add_node(dagent.Node("draft", target=writer, inputs={"prompt": "Draft the report."}))

    invocation = dag.to_dag_spec().nodes[0].payload.invocation
    assert invocation.arguments == {"prompt": "Draft the report."}
    assert dag.agents == [writer]


def test_agent_node_uses_its_own_skill_scope(tmp_path: Path) -> None:
    skill_root = tmp_path / "skills"
    for category, name in (("research", "market"), ("writing", "style")):
        skill_dir = skill_root / category / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} skill.\n---\nUse {name}.",
            encoding="utf-8",
        )
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_1", name="skills_list", arguments={})]),
        ChatResponse(content="researched"),
        ChatResponse(tool_calls=[ToolCall(id="call_2", name="skills_list", arguments={})]),
        ChatResponse(content="written"),
    ])
    researcher = dagent.ToolAgent(
        profile=_profile_root(tmp_path, "researcher"),
        capabilities=[],
        skills=["research/market"],
    )
    writer = dagent.ToolAgent(
        profile=_profile_root(tmp_path, "writer"),
        capabilities=[],
        skills=["writing/style"],
    )
    dag = dagent.Dag("agent_skill_flow")
    research = dagent.Node("research", target=researcher, inputs={"prompt": "Research."})
    write = dagent.Node("write", target=writer, inputs={"prompt": "Write."})
    dag.add_node(research)
    dag.add_node(write)
    dag.add_edge(research, write)
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        skill_roots=[skill_root],
        profile_root=tmp_path / "profiles",
    )

    result = run(runner.run(dag, workspace_root=tmp_path / "runs"))

    assert result.status == "completed"
    research_tool_content = provider.requests[1]["messages"][-1]["content"]
    writer_tool_content = provider.requests[3]["messages"][-1]["content"]
    assert "market" in research_tool_content
    assert "style" not in research_tool_content
    assert "style" in writer_tool_content
    assert "market" not in writer_tool_content


def _profile_root(tmp_path: Path, name: str) -> str:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_path = profile_dir / f"{name}.md"
    profile_path.write_text(f"# {name}\n\nYou are {name}.", encoding="utf-8")
    return name
