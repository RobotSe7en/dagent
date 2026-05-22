import asyncio

import pytest

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


def run(coro):
    return asyncio.run(coro)


def test_package_exposes_tool_and_separate_agent_entrypoints() -> None:
    assert hasattr(dagent, "capability")
    assert not hasattr(dagent, "tool")
    assert hasattr(dagent, "Runner")
    assert hasattr(dagent, "ToolAgent")
    assert hasattr(dagent, "DagAgent")
    assert not hasattr(dagent, "DAgent")
    assert not hasattr(dagent, "run_dag")


def test_capability_decorator_registers_custom_tool_capability() -> None:
    @dagent.capability
    def search(q: str) -> str:
        """Search text."""
        return f"found:{q}"

    assert search.definition.id == "custom_tool.search"
    assert search.definition.name == "search"
    assert search.definition.kind == "custom_tool"


def test_runner_runs_profile_backed_tool_agent_cycle(tmp_path) -> None:
    @dagent.capability
    def search(q: str) -> str:
        return f"found:{q}"

    _profile_root(tmp_path)
    provider = MockProvider([ChatResponse(content="hello")])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["custom_tool.search"],
    )
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        capabilities=[search],
    )

    result = run(runner.run(agent, "hi"))

    assert result.output_text == "hello"
    system_message = provider.requests[0]["messages"][0]["content"]
    assert "You are a conversation profile." in system_message
    assert "search" in system_message


def test_dag_agent_does_not_accept_profile_and_runner_runs_dag_loop(tmp_path) -> None:
    @dagent.capability
    def search(q: str) -> str:
        return f"found:{q}"

    with pytest.raises(TypeError):
        dagent.DagAgent(profile="conversation")

    provider = MockProvider([
        ChatResponse(content='task: research\nlookup = search(q="X")'),
        ChatResponse(content="Report: found:X"),
    ])
    agent = dagent.DagAgent(
        capabilities=["custom_tool.search"],
    )
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        capabilities=[search],
    )

    result = run(runner.run(agent, "research X"))

    assert result.output_text == "Report: found:X"
    assert result.dag is not None
    assert result.dag.nodes[0].payload.invocation.capability_id == "custom_tool.search"


def test_runner_auto_registers_agent_capability_bindings(tmp_path) -> None:
    @dagent.capability
    def search(q: str) -> str:
        return f"found:{q}"

    _profile_root(tmp_path)
    provider = MockProvider([ChatResponse(content="hello")])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=[search],
    )
    runner = dagent.Runner(workspace=tmp_path, provider=provider)

    result = run(runner.run(agent, "hi"))

    assert result.output_text == "hello"
    assert runner.runtime.capability_catalog.get("custom_tool.search") is not None


def test_runner_rejects_unknown_agent_capability_id(tmp_path) -> None:
    _profile_root(tmp_path)
    provider = MockProvider([ChatResponse(content="unused")])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["custom_tool.missing"],
    )
    runner = dagent.Runner(workspace=tmp_path, provider=provider)

    with pytest.raises(KeyError, match="custom_tool.missing"):
        run(runner.run(agent, "hi"))


def test_runner_limits_agent_visible_capabilities(tmp_path) -> None:
    @dagent.capability
    def search(q: str) -> str:
        return f"found:{q}"

    @dagent.capability
    def write(text: str) -> str:
        return f"wrote:{text}"

    _profile_root(tmp_path)
    provider = MockProvider([ChatResponse(content="hello")])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["custom_tool.search"],
    )
    runner = dagent.Runner(workspace=tmp_path, provider=provider, capabilities=[search, write])

    run(runner.run(agent, "hi"))

    system_message = provider.requests[0]["messages"][0]["content"]
    assert "search" in system_message
    assert "write" not in system_message


def test_runner_resume_continues_pending_tool_agent_runtime(tmp_path) -> None:
    @dagent.capability(risk="medium")
    def write(text: str) -> str:
        return f"wrote:{text}"

    _profile_root(tmp_path)
    provider = MockProvider([
        ChatResponse(
            content="",
            tool_calls=[ToolCall(id="call_1", name="write", arguments={"text": "hello"})],
        ),
        ChatResponse(content="done"),
    ])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[write], review="careful")
    runner = dagent.Runner(workspace=tmp_path, provider=provider)

    first = run(runner.run(agent, "write hello"))
    assert first.requires_review
    assert first.review is not None

    resumed = run(runner.resume(first.review.approve()))

    assert resumed is not None
    assert resumed.output_text == "done"


def test_runner_rejects_conflicting_capability_registration(tmp_path) -> None:
    @dagent.capability(id="custom_tool.same", name="same")
    def first() -> str:
        return "first"

    @dagent.capability(id="custom_tool.same", name="same")
    def second() -> str:
        return "second"

    runner = dagent.Runner(workspace=tmp_path, capabilities=[first])

    with pytest.raises(ValueError, match="custom_tool.same"):
        runner.add_capability(second)


def _profile_root(tmp_path):
    profiles = tmp_path / "profiles"
    conversation = profiles / "conversation"
    conversation.mkdir(parents=True)
    (conversation / "profile.yaml").write_text(
        "\n".join([
            "name: conversation",
            "role: conversation",
            "layers:",
            "  - agent.md",
        ]),
        encoding="utf-8",
    )
    (conversation / "agent.md").write_text("You are a conversation profile.", encoding="utf-8")
    return str(conversation)
