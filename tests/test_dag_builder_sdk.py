import asyncio
from pathlib import Path

import pytest

import dagent
from dagent.providers import ChatResponse, MockProvider
from dagent.schemas import DAGSpec


def run(coro):
    return asyncio.run(coro)


def test_dag_builder_creates_capability_nodes_edges_and_refs() -> None:
    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    @dagent.tool(risk="medium")
    def write_file(path: str, content: str) -> str:
        return f"wrote:{path}:{content}"

    dag = dagent.Dag("research_report", name="Research Report")
    report = dag.artifact("report", "outputs/report.md")
    search_node = dag.capability_node("search", search, q="dagent sdk")
    write_node = dag.capability_node(
        "write_report",
        write_file,
        path=report.path,
        content=search_node.output,
        outputs=[report],
    ).after(search_node)

    spec = dag.to_dag_spec()

    assert isinstance(spec, DAGSpec)
    assert spec.id == "research_report"
    assert spec.name == "Research Report"
    assert spec.artifacts["report"].paths == ["outputs/report.md"]
    assert [node.id for node in spec.nodes] == ["search", "write_report"]
    assert spec.nodes[0].payload.invocation.capability_id == "tool.search"
    assert spec.nodes[1].payload.invocation.arguments == {
        "path": "{{artifact.report.path}}",
        "content": "{{search.output}}",
    }
    assert spec.nodes[1].outputs == ["report"]
    assert [(edge.source, edge.target) for edge in spec.edges] == [("search", "write_report")]
    assert write_node.id == "write_report"


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

    runner = dagent.Runner(workspace=tmp_path)
    result = run(runner.run(dag, workspace_root=tmp_path / "runs"))

    assert result.status == "completed"
    assert result.trace.artifacts["note"].status == "created"
    workspace = Path(result.workspace_path)
    assert (workspace / "notes" / "output.txt").read_text(encoding="utf-8") == "hi"


def test_agent_node_generates_agent_capability_invocation(tmp_path: Path) -> None:
    provider = MockProvider([ChatResponse(content="drafted")])
    writer = dagent.ToolAgent(
        profile=_profile_root(tmp_path, "writer"),
    )
    dag = dagent.Dag("agent_flow")
    draft = dag.agent_node("draft", writer, prompt="Draft the report.", max_steps=3)

    spec = dag.to_dag_spec()

    invocation = spec.nodes[0].payload.invocation
    assert draft.output == "{{draft.output}}"
    assert invocation.capability_id == "agent.writer"
    assert invocation.kind == "agent"
    assert invocation.arguments == {"prompt": "Draft the report.", "max_steps": 3}

    runner = dagent.Runner(workspace=tmp_path, provider=provider)
    result = run(runner.run(dag, workspace_root=tmp_path / "runs"))
    assert result.status == "completed"


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
    profile_dir = tmp_path / "profiles" / name
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile.yaml").write_text(
        "\n".join([
            f"name: {name}",
            "role: agent",
            "layers:",
            "  - agent.md",
        ]),
        encoding="utf-8",
    )
    (profile_dir / "agent.md").write_text(f"You are {name}.", encoding="utf-8")
    return str(profile_dir)
