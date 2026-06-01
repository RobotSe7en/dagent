import asyncio
import inspect
from pathlib import Path

import pytest

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


def run(coro):
    return asyncio.run(coro)


def test_package_exposes_tool_and_separate_agent_entrypoints() -> None:
    assert hasattr(dagent, "tool")
    assert hasattr(dagent.capabilities, "tool")
    assert not hasattr(dagent, "capability")
    assert not hasattr(dagent.capabilities, "capability")
    assert hasattr(dagent, "Runner")
    assert hasattr(dagent, "ToolAgent")
    assert hasattr(dagent, "DagAgent")
    assert hasattr(dagent, "ArtifactUpload")
    assert hasattr(dagent, "CapabilityScope")
    assert hasattr(dagent, "ProfileStore")
    assert hasattr(dagent, "ReviewLevel")
    assert hasattr(dagent, "RuntimeMode")
    assert hasattr(dagent, "SkillStore")
    assert hasattr(dagent, "validate_dag_spec")
    assert not hasattr(dagent, "DAgent")
    assert not hasattr(dagent, "run_dag")


def test_tool_decorator_has_tool_only_signature() -> None:
    assert "kind" not in inspect.signature(dagent.tool).parameters
    assert "manager" not in inspect.signature(dagent.Runner.add_mcp_server).parameters


def test_tool_decorator_matches_tool_default_kind() -> None:
    @dagent.tool
    def search(q: str) -> str:
        """Search text."""
        return f"found:{q}"

    @dagent.tool(risk="medium")
    def write_file(path: str, content: str) -> str:
        return f"wrote:{path}"

    assert search.definition.id == "tool.search"
    assert search.definition.kind == "tool"
    assert search.definition.policy.risk == "low"
    assert write_file.definition.id == "tool.write_file"
    assert write_file.definition.policy.risk == "medium"


def test_tool_decorator_registers_tool_capability() -> None:
    @dagent.tool
    def search(q: str) -> str:
        """Search text."""
        return f"found:{q}"

    assert search.definition.id == "tool.search"
    assert search.definition.name == "search"
    assert search.definition.kind == "tool"


def test_runner_runs_profile_backed_tool_agent_cycle(tmp_path) -> None:
    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    _profile_root(tmp_path)
    provider = MockProvider([ChatResponse(content="hello")])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["tool.search"],
    )
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        capabilities=[search],
        profile_root=tmp_path / "profiles",
    )

    result = run(runner.run(agent, "hi"))

    assert result.output_text == "hello"
    system_message = provider.requests[0]["messages"][0]["content"]
    assert "You are a conversation profile." in system_message
    assert "search" in system_message


def test_runner_loads_builtin_profile_without_cwd_profiles(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="hello")])
    runner = dagent.Runner(workspace=tmp_path, provider=provider)

    result = run(runner.run(dagent.ToolAgent(profile="conversation"), "hi"))

    assert result.output_text == "hello"
    system_message = provider.requests[0]["messages"][0]["content"]
    assert "Conversation Agent" in system_message


def test_dag_agent_does_not_accept_profile_and_runner_runs_dag_loop(tmp_path) -> None:
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
        capabilities=["tool.search"],
    )
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        capabilities=[search],
    )

    result = run(runner.run(agent, "research X"))

    assert result.output_text == "Report: found:X"
    assert result.dag is not None
    assert result.dag.nodes[0].payload.invocation.capability_id == "tool.search"


def test_runner_auto_registers_agent_capability_bindings(tmp_path) -> None:
    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    _profile_root(tmp_path)
    provider = MockProvider([ChatResponse(content="hello")])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=[search],
    )
    runner = dagent.Runner(workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    result = run(runner.run(agent, "hi"))

    assert result.output_text == "hello"
    assert runner.runtime.capability_catalog.get("tool.search") is not None


def test_runner_rejects_unknown_agent_capability_id(tmp_path) -> None:
    _profile_root(tmp_path)
    provider = MockProvider([ChatResponse(content="unused")])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["tool.missing"],
    )
    runner = dagent.Runner(workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    with pytest.raises(KeyError, match="tool.missing"):
        run(runner.run(agent, "hi"))


def test_runner_limits_agent_visible_capabilities(tmp_path) -> None:
    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    @dagent.tool
    def write(text: str) -> str:
        return f"wrote:{text}"

    _profile_root(tmp_path)
    provider = MockProvider([ChatResponse(content="hello")])
    agent = dagent.ToolAgent(
        profile="conversation",
        capabilities=["tool.search"],
    )
    runner = dagent.Runner(
        workspace=tmp_path,
        provider=provider,
        capabilities=[search, write],
        profile_root=tmp_path / "profiles",
    )

    run(runner.run(agent, "hi"))

    system_message = provider.requests[0]["messages"][0]["content"]
    assert "search" in system_message
    assert "write" not in system_message


def test_runner_default_agent_capabilities_exclude_registered_agent_capabilities(tmp_path) -> None:
    _profile_root(tmp_path, "writer")
    _profile_root(tmp_path, "conversation")
    provider = MockProvider([
        ChatResponse(content="drafted"),
        ChatResponse(content="hello"),
    ])
    writer = dagent.ToolAgent(profile="writer")
    dag = dagent.Dag("agent_flow")
    dag.agent_node("draft", writer, prompt="Draft the report.")
    runner = dagent.Runner(workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    run(runner.run(dag, workspace_root=tmp_path / "runs"))
    result = run(runner.run(dagent.ToolAgent(profile="conversation"), "hi"))

    assert result.output_text == "hello"
    assert "writer" not in _tool_names(provider.requests[-1])


def test_runner_resume_continues_pending_tool_agent_runtime(tmp_path) -> None:
    @dagent.tool(risk="medium")
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
    runner = dagent.Runner(workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    first = run(runner.run(agent, "write hello"))
    assert first.requires_review
    assert first.review is not None

    resumed = run(runner.resume(first.review.approve()))

    assert resumed is not None
    assert resumed.output_text == "done"


def test_runner_invalid_dag_resume_does_not_consume_pending_runtime(tmp_path) -> None:
    _profile_root(tmp_path, "planner")
    provider = MockProvider([
        ChatResponse(content='task: research\nlookup = search(q="X")'),
        ChatResponse(content="Report: found:X"),
    ])

    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    agent = dagent.DagAgent(planner_profile="planner", capabilities=[search], review="careful")
    runner = dagent.Runner(workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    first = run(runner.run(agent, "research X"))
    assert first.requires_review
    assert first.review is not None

    missing_dag = dagent.ReviewDecision(review_id=first.review.id, approved=True)
    assert run(runner.resume(missing_dag)) is None

    resumed = run(runner.resume(first.review.approve()))

    assert resumed is not None
    assert resumed.output_text == "Report: found:X"
    resume_system_message = provider.requests[-1]["messages"][0]["content"]
    assert "You are a planner profile." in resume_system_message


def test_runner_rejects_conflicting_capability_registration(tmp_path) -> None:
    @dagent.tool(id="tool.same", name="same")
    def first() -> str:
        return "first"

    @dagent.tool(id="tool.same", name="same")
    def second() -> str:
        return "second"

    runner = dagent.Runner(workspace=tmp_path, provider=MockProvider([]), capabilities=[first])

    with pytest.raises(ValueError, match="tool.same"):
        runner.add_tool(second)


def test_runner_close_shuts_down_capability_resources(tmp_path) -> None:
    closed: list[str] = []
    provider = MockProvider([])
    runner = dagent.Runner(workspace=tmp_path, provider=provider)
    runner.runtime.capability_catalog.add_shutdown_hook(lambda: closed.append("closed"))

    runner.close()
    runner.close()

    assert closed == ["closed"]


def test_runner_with_injected_provider_allows_missing_config(tmp_path, monkeypatch) -> None:
    provider = MockProvider([])
    monkeypatch.setenv("DAGENT_CONFIG", str(tmp_path / "missing.yaml"))

    runner = dagent.Runner(workspace=tmp_path, provider=provider)

    assert runner.runtime.provider is provider


def test_runner_with_injected_provider_ignores_invalid_config(tmp_path, monkeypatch) -> None:
    bad_config = tmp_path / "config.yaml"
    bad_config.write_text("provider: [", encoding="utf-8")
    monkeypatch.setenv("DAGENT_CONFIG", str(bad_config))

    provider = MockProvider([])
    runner = dagent.Runner(workspace=tmp_path, provider=provider)

    assert runner.runtime.provider is provider


def test_runner_requires_explicit_provider(tmp_path) -> None:
    with pytest.raises(ValueError, match="No provider configured"):
        dagent.Runner(workspace=tmp_path)


def test_runner_from_config_loads_provider_and_profile_root(tmp_path) -> None:
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "conversation.md").write_text("# Config Conversation\n\nFrom config.", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join([
            "provider:",
            "  base_url: https://example.test/v1",
            "  model: cfg-model",
            "  api_key: test-key",
            "profiles:",
            f"  directory: {profiles}",
        ]),
        encoding="utf-8",
    )

    runner = dagent.Runner.from_config(config, workspace=tmp_path)

    assert runner.runtime.provider.config.model == "cfg-model"
    assert runner.runtime.tool_agent.profile.content.startswith("# Config Conversation")


def test_runner_from_config_uses_builtin_profiles_without_profile_directory(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join([
            "provider:",
            "  base_url: https://example.test/v1",
            "  model: cfg-model",
            "  api_key: test-key",
        ]),
        encoding="utf-8",
    )

    runner = dagent.Runner.from_config(config, workspace=tmp_path)

    assert runner.runtime.tool_agent.profile.name == "conversation"


def _profile_root(tmp_path, name: str = "conversation"):
    profiles = tmp_path / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    profile = profiles / f"{name}.md"
    profile.write_text(f"# {name}\n\nYou are a {name} profile.", encoding="utf-8")
    return str(profile)


def _tool_names(request: dict) -> set[str]:
    return {
        tool["function"]["name"]
        for tool in request["tools"]
        if tool.get("type") == "function"
    }
