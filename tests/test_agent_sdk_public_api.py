import asyncio

import pytest

import dagent
from dagent.providers import ChatResponse, MockProvider


def run(coro):
    return asyncio.run(coro)


def test_package_exposes_tool_and_separate_agent_entrypoints() -> None:
    assert hasattr(dagent, "tool")
    assert hasattr(dagent, "ToolAgent")
    assert hasattr(dagent, "DagAgent")
    assert not hasattr(dagent, "DAgent")
    assert not hasattr(dagent, "Runner")


def test_tool_decorator_registers_custom_tool_capability() -> None:
    @dagent.tool
    def search(q: str) -> str:
        """Search text."""
        return f"found:{q}"

    assert search.definition.id == "custom_tool.search"
    assert search.definition.name == "search"
    assert search.definition.kind == "custom_tool"


def test_tool_agent_runs_profile_backed_capability_cycle(tmp_path) -> None:
    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    _profile_root(tmp_path)
    provider = MockProvider([ChatResponse(content="hello")])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=[search],
        workspace=tmp_path,
        provider=provider,
    )

    result = run(agent.run("hi"))

    assert result.output_text == "hello"
    system_message = provider.requests[0]["messages"][0]["content"]
    assert "You are a conversation profile." in system_message
    assert "search" in system_message


def test_dag_agent_does_not_accept_profile_and_runs_dag_loop(tmp_path) -> None:
    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    with pytest.raises(TypeError):
        dagent.DagAgent(profile="conversation")

    provider = MockProvider([
        ChatResponse(content='task: research\nlookup = search(q="X")'),
        ChatResponse(content="Report: found:X"),
    ])
    agent = dagent.DagAgent(
        capabilities=[search],
        workspace=tmp_path,
        provider=provider,
    )

    result = run(agent.run("research X"))

    assert result.output_text == "Report: found:X"
    assert result.dag is not None
    assert result.dag.nodes[0].payload.invocation.capability_id == "custom_tool.search"


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
