import asyncio
from pathlib import Path

import pytest

import dagent
import dagent.runner as runner_module
from dagent.capabilities.tools.registry import ToolOutput
from dagent.harness_runtime.artifacts import create_run_workspace
from dagent.providers import ChatResponse, MockProvider, ToolCall
from tests.planner_helpers import capability_plan_response, final_answer_response


def run(coro):
    return asyncio.run(coro)


def test_runner_defaults_to_managed_dagent_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    runner = dagent.Runner(provider=MockProvider([]))

    assert runner.workspace == Path(".dagent")
    assert runner.runtime.capability_catalog.workspace_root == tmp_path / ".dagent"


def test_tool_agent_uses_run_workspace_for_relative_tool_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="tool_write_file",
                    arguments={"path": "shared/tool.txt", "content": "hi"},
                )
            ]
        ),
        ChatResponse(content="done"),
    ])
    runner = dagent.Runner(provider=provider)

    result = run(
        runner.run(
            dagent.ToolAgent(profile="conversation"),
            input="write a tool note",
        )
    )

    assert result.workspace_path is not None
    workspace_path = Path(result.workspace_path)
    assert workspace_path.parent == tmp_path / ".dagent" / "runs"
    assert (workspace_path / "shared" / "tool.txt").read_text(encoding="utf-8") == "hi"
    assert not (tmp_path / ".dagent" / "shared" / "tool.txt").exists()


def test_runner_uses_resolved_workspace_after_cwd_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    monkeypatch.chdir(project)
    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="tool_write_file",
                    arguments={"path": "shared/chdir.txt", "content": "hi"},
                )
            ]
        ),
        ChatResponse(content="done"),
    ])
    runner = dagent.Runner(provider=provider)

    monkeypatch.chdir(other)
    result = run(
        runner.run(
            dagent.ToolAgent(profile="conversation"),
            input="write after cwd changed",
        )
    )

    assert not (other / ".dagent").exists()
    assert result.workspace_path is not None
    workspace_path = Path(result.workspace_path)
    assert workspace_path.parent == project / ".dagent" / "runs"
    assert (workspace_path / "shared" / "chdir.txt").read_text(encoding="utf-8") == "hi"
    assert not (project / ".dagent" / "shared" / "chdir.txt").exists()


def test_dag_agent_uses_run_workspace_for_relative_tool_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    provider = MockProvider([
        ChatResponse(content=capability_plan_response(
            "tool.write_file",
            {"path": "shared/dag.txt", "content": "hi"},
            node_id="write",
        )),
        ChatResponse(content=final_answer_response("done")),
    ])
    runner = dagent.Runner(provider=provider)

    result = run(
        runner.run(
            dagent.DagAgent(),
            input="write a dag note",
        )
    )

    assert result.workspace_path is not None
    workspace_path = Path(result.workspace_path)
    assert workspace_path.parent == tmp_path / ".dagent" / "runs"
    system_prompt = provider.requests[0]["messages"][0]["content"]
    assert "## Runtime Context" in system_prompt
    assert f"- Workspace root: {workspace_path.resolve()}" in system_prompt
    assert (workspace_path / "shared" / "dag.txt").read_text(encoding="utf-8") == "hi"
    assert not (tmp_path / ".dagent" / "shared" / "dag.txt").exists()


def test_tool_agent_conversation_continuation_uses_a_new_run_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="tool_write_file",
                    arguments={"path": "shared/first.txt", "content": "one"},
                )
            ]
        ),
        ChatResponse(content="first done"),
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_2",
                    name="tool_write_file",
                    arguments={"path": "shared/second.txt", "content": "two"},
                )
            ]
        ),
        ChatResponse(content="second done"),
    ])
    runner = dagent.Runner(provider=provider)
    agent = dagent.ToolAgent(profile="conversation")

    first = run(
        runner.run(
            agent,
            input="write first",
        )
    )
    second = run(
        runner.run(
            agent,
            input="write second",
            conversation=first.conversation,
        )
    )

    assert second.run_id != first.run_id
    assert second.workspace_path != first.workspace_path
    first_workspace = Path(first.workspace_path)
    second_workspace = Path(second.workspace_path)
    assert (first_workspace / "shared" / "first.txt").read_text(encoding="utf-8") == "one"
    assert not (first_workspace / "shared" / "second.txt").exists()
    assert not (second_workspace / "shared" / "first.txt").exists()
    assert (second_workspace / "shared" / "second.txt").read_text(encoding="utf-8") == "two"
    assert not (tmp_path / ".dagent" / "shared" / "first.txt").exists()
    assert not (tmp_path / ".dagent" / "shared" / "second.txt").exists()


def test_tool_agent_can_use_exact_workspace_path_without_run_subdirectory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "project-workspace"
    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="tool_write_file",
                    arguments={"path": "shared/project.txt", "content": "hi"},
                )
            ]
        ),
        ChatResponse(content="done"),
    ])
    runner = dagent.Runner(provider=provider)

    result = run(
        runner.run(
            dagent.ToolAgent(profile="conversation"),
            input="write in project workspace",
            workspace_path=workspace,
        )
    )

    assert result.workspace_path is not None
    assert Path(result.workspace_path) == workspace.resolve()
    system_prompt = provider.requests[0]["messages"][0]["content"]
    assert "## Runtime Context" in system_prompt
    assert f"- Workspace root: {workspace.resolve()}" in system_prompt
    assert (workspace / "shared" / "project.txt").read_text(encoding="utf-8") == "hi"
    assert not (workspace / result.run_id).exists()


def test_conversation_continuation_can_choose_a_new_exact_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    provider = MockProvider([
        ChatResponse(content="first done"),
        ChatResponse(content="second done"),
    ])
    runner = dagent.Runner(provider=provider)
    agent = dagent.ToolAgent(profile="conversation")

    first = run(
        runner.run(
            agent,
            input="first",
            workspace_path=first_workspace,
        )
    )

    second = run(
        runner.run(
            agent,
            input="second",
            conversation=first.conversation,
            workspace_path=second_workspace,
        )
    )

    assert Path(first.workspace_path) == first_workspace
    assert Path(second.workspace_path) == second_workspace
    assert first.run_id != second.run_id


def test_dag_agent_conversation_continuation_uses_a_new_run_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    provider = MockProvider([
        ChatResponse(content=capability_plan_response(
            "tool.write_file",
            {"path": "shared/dag_first.txt", "content": "one"},
            node_id="write",
        )),
        ChatResponse(content=final_answer_response("first done")),
        ChatResponse(content=capability_plan_response(
            "tool.write_file",
            {"path": "shared/dag_second.txt", "content": "two"},
            node_id="write",
        )),
        ChatResponse(content=final_answer_response("second done")),
    ])
    runner = dagent.Runner(provider=provider)
    agent = dagent.DagAgent()

    first = run(
        runner.run(
            agent,
            input="write first dag note",
        )
    )
    second = run(
        runner.run(
            agent,
            input="write second dag note",
            conversation=first.conversation,
        )
    )

    assert second.run_id != first.run_id
    assert second.workspace_path != first.workspace_path
    first_workspace = Path(first.workspace_path)
    second_workspace = Path(second.workspace_path)
    assert (first_workspace / "shared" / "dag_first.txt").read_text(encoding="utf-8") == "one"
    assert not (first_workspace / "shared" / "dag_second.txt").exists()
    assert not (second_workspace / "shared" / "dag_first.txt").exists()
    assert (second_workspace / "shared" / "dag_second.txt").read_text(encoding="utf-8") == "two"
    assert not (tmp_path / ".dagent" / "shared" / "dag_first.txt").exists()
    assert not (tmp_path / ".dagent" / "shared" / "dag_second.txt").exists()


def test_static_dag_artifacts_live_under_dagent_runs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    dag = dagent.Dag("write_note")
    note = dag.artifact("note", "notes/output.txt")
    dag.add_node(
        dagent.Node(
            "write",
            target="tool.write_file",
            inputs={"path": note.path, "content": "hi"},
            artifact_outputs=[note],
            boundary=dagent.Boundary(allowed_paths=[note.path.as_expr()]),
        )
    )
    runner = dagent.Runner(provider=MockProvider([]))

    result = run(runner.run(dag))

    assert result.workspace_path is not None
    workspace_path = Path(result.workspace_path)
    assert workspace_path.parent == tmp_path / ".dagent" / "runs"
    assert (workspace_path / "notes" / "output.txt").read_text(encoding="utf-8") == "hi"
    invocation = result.trace.root.children[0].children[0].capability_execution.invocation
    assert invocation.arguments["path"] == "notes/output.txt"
    assert not (tmp_path / ".dagent" / "notes" / "output.txt").exists()


def test_static_dag_can_use_exact_workspace_path_without_run_subdirectory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "project-workspace"
    dag = dagent.Dag("write_note")
    note = dag.artifact("note", "notes/project-output.txt")
    dag.add_node(
        dagent.Node(
            "write",
            target="tool.write_file",
            inputs={"path": note.path, "content": "hi"},
            artifact_outputs=[note],
            boundary=dagent.Boundary(allowed_paths=[note.path.as_expr()]),
        )
    )
    runner = dagent.Runner(provider=MockProvider([]))

    result = run(runner.run(dag, workspace_path=workspace))

    assert result.workspace_path is not None
    assert Path(result.workspace_path) == workspace.resolve()
    assert (workspace / "notes" / "project-output.txt").read_text(encoding="utf-8") == "hi"
    assert not (workspace / result.run_id).exists()


def test_static_dag_uses_resolved_workspace_after_cwd_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    monkeypatch.chdir(project)
    runner = dagent.Runner(provider=MockProvider([]))
    dag = dagent.Dag("write_note")
    note = dag.artifact("note", "notes/output.txt")
    dag.add_node(
        dagent.Node(
            "write",
            target="tool.write_file",
            inputs={"path": note.path, "content": "hi"},
            artifact_outputs=[note],
            boundary=dagent.Boundary(allowed_paths=[note.path.as_expr()]),
        )
    )

    monkeypatch.chdir(other)
    result = run(runner.run(dag))

    assert result.workspace_path is not None
    workspace_path = Path(result.workspace_path)
    assert workspace_path.parent == project / ".dagent" / "runs"
    assert (workspace_path / "notes" / "output.txt").read_text(encoding="utf-8") == "hi"
    assert not (other / ".dagent").exists()


def test_sandbox_tool_run_records_run_id_workspace_and_mounts_dagent_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    class FakeSandboxSession:
        instances = []

        def __init__(self, _config, *, workspace_root: Path, skill_dirs=()) -> None:
            self.workspace_root = Path(workspace_root).resolve()
            self.skill_dirs = tuple(skill_dirs)
            self.closed = False
            FakeSandboxSession.instances.append(self)

        def start(self) -> None:
            self.workspace_root.mkdir(parents=True, exist_ok=True)

        def close(self) -> None:
            self.closed = True

        def run_tool(self, tool_name: str, arguments: dict) -> ToolOutput:
            assert tool_name == "write_file"
            path = Path(arguments["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(arguments["content"], encoding="utf-8")
            return ToolOutput(content=f"wrote:{path}", value={"path": str(path)})

    monkeypatch.setattr(runner_module, "SandboxSession", FakeSandboxSession)
    provider = MockProvider([
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="tool_write_file",
                    arguments={"path": "shared/sandbox.txt", "content": "hi"},
                )
            ]
        ),
        ChatResponse(content="done"),
    ])
    runner = dagent.Runner(provider=provider)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)

    result = run(
        runner.run(
            dagent.ToolAgent(profile="conversation"),
            input="write a sandbox note",
            execution="sandbox",
        )
    )

    assert FakeSandboxSession.instances[0].workspace_root == tmp_path / ".dagent"
    assert FakeSandboxSession.instances[0].closed is True
    assert result.workspace_path is not None
    workspace_path = Path(result.workspace_path)
    assert workspace_path.parent == tmp_path / ".dagent" / "runs"
    assert workspace_path.name == result.run_id
    assert (workspace_path / "shared" / "sandbox.txt").read_text(encoding="utf-8") == "hi"
    assert not (tmp_path / ".dagent" / "shared" / "sandbox.txt").exists()


def test_sandbox_rejects_exact_workspace_path_outside_runner_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    class FakeSandboxSession:
        def __init__(self, _config, *, workspace_root: Path, skill_dirs=()) -> None:
            self.workspace_root = Path(workspace_root).resolve()

        def start(self) -> None:
            self.workspace_root.mkdir(parents=True, exist_ok=True)

        def close(self) -> None:
            pass

        def run_tool(self, tool_name: str, arguments: dict) -> ToolOutput:
            return ToolOutput(content="unused")

    monkeypatch.setattr(runner_module, "SandboxSession", FakeSandboxSession)
    runner = dagent.Runner(provider=MockProvider([ChatResponse(content="done")]))

    with pytest.raises(runner_module.SandboxExecutionError, match="workspace_path"):
        run(
            runner.run(
                dagent.ToolAgent(profile="conversation"),
                input="write outside",
                execution="sandbox",
                workspace_path=tmp_path / "outside-workspace",
            )
        )


def test_create_run_workspace_rejects_non_leaf_run_id(tmp_path: Path) -> None:
    for bad_run_id in ("", ".", "..", "../escape", "nested/run", "/tmp/run", r"nested\run"):
        with pytest.raises(ValueError, match="run_id"):
            create_run_workspace(tmp_path / "runs", run_id=bad_run_id)


def test_static_dag_example_writes_artifact_to_run_workspace(capsys) -> None:
    from examples import static_dag

    run(static_dag.main())

    output = capsys.readouterr().out
    assert "completed" in output
    assert "found:dagent sdk" in output
