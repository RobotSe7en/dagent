import asyncio
import inspect
from pathlib import Path

import pytest

import dagent
from dagent.providers import ChatResponse, MockProvider, ToolCall


def run(coro):
    return asyncio.run(coro)


def user_messages(content: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": content}]


def test_package_exposes_tool_and_separate_agent_entrypoints() -> None:
    assert hasattr(dagent, "tool")
    assert hasattr(dagent.capabilities, "tool")
    assert not hasattr(dagent, "capability")
    assert not hasattr(dagent.capabilities, "capability")
    assert hasattr(dagent, "Runner")
    assert hasattr(dagent, "AutoAgent")
    assert hasattr(dagent, "ToolAgent")
    assert hasattr(dagent, "DagAgent")
    assert hasattr(dagent, "ArtifactUpload")
    assert hasattr(dagent, "CapabilityScope")
    assert hasattr(dagent, "ProfileStore")
    assert hasattr(dagent, "Provider")
    assert hasattr(dagent, "ReviewLevel")
    assert hasattr(dagent, "RunStreamChunk")
    assert hasattr(dagent, "RunStreamEvent")
    assert hasattr(dagent, "SkillStore")
    assert hasattr(dagent, "load_builtin_profile")
    assert hasattr(dagent, "list_builtin_profiles")
    assert hasattr(dagent, "validate_dag_spec")
    assert hasattr(dagent, "Node")
    assert hasattr(dagent.Runner, "stream")
    assert hasattr(dagent.Runner, "stream_events")
    assert hasattr(dagent.Runner, "resume_stream")
    assert hasattr(dagent.Runner, "resume_stream_events")
    assert not hasattr(dagent, "NodeRef")
    assert not hasattr(dagent.Dag, "capability_node")
    assert not hasattr(dagent.Dag, "agent_node")
    assert not hasattr(dagent, "DAgent")
    assert not hasattr(dagent, "OpenAICompatibleProvider")
    assert not hasattr(dagent, "ProviderConfig")
    assert not hasattr(dagent, "RuntimeMode")
    assert not hasattr(dagent, "run_dag")


def test_auto_agent_is_public_target_without_mode_field() -> None:
    assert "mode" not in inspect.signature(dagent.AutoAgent).parameters


def test_builtin_profiles_are_available_from_package_root() -> None:
    profile = dagent.load_builtin_profile("conversation")

    assert profile.name == "conversation"
    assert "Conversation Agent" in profile.content
    assert "conversation" in {item.name for item in dagent.list_builtin_profiles()}


def test_provider_is_public_from_package_root() -> None:
    provider = dagent.Provider(
        base_url="https://example.test/v1",
        model="test-model",
        api_key="test-key",
    )

    assert provider.config.base_url == "https://example.test/v1"
    assert provider.config.model == "test-model"
    assert provider.config.api_key == "test-key"
    assert "config" not in inspect.signature(dagent.Provider).parameters


def test_tool_decorator_has_tool_only_signature() -> None:
    assert "kind" not in inspect.signature(dagent.tool).parameters
    assert "manager" not in inspect.signature(dagent.Runner.add_mcp_server).parameters
    assert hasattr(dagent.Runner, "remove_mcp_server")
    assert hasattr(dagent.Runner, "replace_mcp_server")
    assert "manager" not in inspect.signature(dagent.Runner.replace_mcp_server).parameters


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

    result = run(runner.run(agent, messages=user_messages("hi")))

    assert result.output_text == "hello"
    system_message = provider.requests[0]["messages"][0]["content"]
    assert "You are a conversation profile." in system_message
    assert "search" in system_message


def test_runner_loads_builtin_profile_without_cwd_profiles(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="hello")])
    runner = dagent.Runner(workspace=tmp_path, provider=provider)

    result = run(runner.run(dagent.ToolAgent(profile="conversation"), messages=user_messages("hi")))

    assert result.output_text == "hello"
    system_message = provider.requests[0]["messages"][0]["content"]
    assert "Conversation Agent" in system_message


def test_runner_stream_yields_chunks_and_unified_result(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="<think>checking</think>hello")])
    runner = dagent.Runner(workspace=tmp_path, provider=provider)

    async def collect() -> list[dagent.RunStreamChunk]:
        return [
            chunk
            async for chunk in runner.stream(
                dagent.ToolAgent(profile="conversation"),
                messages=user_messages("hi"),
            )
        ]

    chunks = run(collect())

    assert [chunk.text for chunk in chunks if chunk.text] == ["<think>checking</think>hello"]
    assert chunks[-1].result is not None
    assert isinstance(chunks[-1].result, dagent.RunResult)
    assert chunks[-1].result.output_text == "hello"
    assert chunks[-1].event is not None
    assert chunks[-1].event.type == "run.finished"


def test_runner_stream_text_stream_selects_token_channel(tmp_path) -> None:
    async def collect(text_stream: str) -> list[str]:
        provider = MockProvider([ChatResponse(content="<think>checking</think>hello")])
        runner = dagent.Runner(workspace=tmp_path, provider=provider)
        return [
            chunk.text
            async for chunk in runner.stream(
                dagent.ToolAgent(profile="conversation"),
                messages=user_messages("hi"),
                text_stream=text_stream,
            )
            if chunk.text
        ]

    assert run(collect("raw")) == ["<think>checking</think>hello"]
    assert run(collect("reasoning")) == ["checking"]
    assert run(collect("content")) == ["hello"]
    assert run(collect("none")) == []


def test_run_result_and_stream_event_model_dump_are_json_ready(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="<think>checking</think>hello")])
    runner = dagent.Runner(workspace=tmp_path, provider=provider)

    async def collect() -> list[dagent.RunStreamEvent]:
        return [
            event
            async for event in runner.stream_events(
                dagent.ToolAgent(profile="conversation"),
                messages=user_messages("hi"),
            )
        ]

    events = run(collect())
    result = events[-1].data.result

    assert result is not None
    assert result.model_dump(mode="json")["output_text"] == "hello"
    token_events = [
        event.model_dump(mode="json")
        for event in events
        if event.type.startswith("response.")
    ]
    assert token_events == [
        {
            "type": "response.raw.delta",
            "data": {"delta": "<think>checking</think>hello"},
            "sequence": 1,
            "run_id": None,
        },
        {
            "type": "response.reasoning.delta",
            "data": {"delta": "checking"},
            "sequence": 2,
            "run_id": None,
        },
        {
            "type": "response.content.delta",
            "data": {"delta": "hello"},
            "sequence": 3,
            "run_id": None,
        },
    ]
    assert events[-1].model_dump(mode="json")["data"]["result"]["output_text"] == "hello"


def test_runner_stream_yields_typed_status_events_and_errors(tmp_path) -> None:
    runner = dagent.Runner(workspace=tmp_path, provider=MockProvider([]))
    events: list[dagent.RunStreamEvent] = []

    async def collect() -> None:
        async for event in runner.stream_events(dagent.ToolAgent(profile="conversation")):
            events.append(event)

    with pytest.raises(TypeError, match="messages is required"):
        run(collect())

    assert events[-1].type == "run.failed"
    assert events[-1].data.message == "messages is required for ToolAgent targets."
    assert events[-1].data.error_type == "TypeError"


def test_runner_auto_agent_routes_to_tool_result(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(content="tool"),
        ChatResponse(content="hello from tool"),
    ])
    agent = dagent.AutoAgent(capabilities=[], skills=[])
    runner = dagent.Runner(workspace=tmp_path, provider=provider)

    result = run(runner.run(agent, messages=user_messages("hi")))

    assert result.kind == "tool"
    assert result.output_text == "hello from tool"


def test_runner_auto_agent_routes_to_dynamic_dag_result(tmp_path) -> None:
    @dagent.tool
    def search(q: str) -> str:
        return f"found:{q}"

    provider = MockProvider([
        ChatResponse(content="dag"),
        ChatResponse(content='task: research\nlookup = search(q="X")'),
        ChatResponse(content="Report: found:X"),
    ])
    agent = dagent.AutoAgent(capabilities=[search], skills=[])
    runner = dagent.Runner(workspace=tmp_path, provider=provider)

    result = run(runner.run(agent, messages=user_messages("research X")))

    assert result.kind == "dynamic_dag"
    assert result.output_text == "Report: found:X"
    assert result.dag is not None
    assert result.dag.nodes[0].payload.invocation.capability_id == "tool.search"


def test_runner_resume_stream_continues_pending_review(tmp_path) -> None:
    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        return f"wrote:{text}"

    _profile_root(tmp_path)
    provider = MockProvider([
        ChatResponse(
            content="",
            tool_calls=[ToolCall(id="call_1", name="write", arguments={"text": "hello"})],
        ),
        ChatResponse(content="<think>checking</think>done"),
    ])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[write], review="careful")
    runner = dagent.Runner(workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    first = run(runner.run(agent, messages=user_messages("write hello")))
    assert first.requires_review
    assert first.review is not None

    async def collect() -> list[dagent.RunStreamChunk]:
        return [chunk async for chunk in runner.resume_stream(first.review.approve())]

    chunks = run(collect())

    assert [chunk.text for chunk in chunks if chunk.text] == ["<think>checking</think>done"]
    assert chunks[-1].result is not None
    assert chunks[-1].result.output_text == "done"
    assert chunks[-1].event is not None
    assert chunks[-1].event.type == "run.finished"


def test_runner_resume_stream_text_stream_selects_content_channel(tmp_path) -> None:
    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        return f"wrote:{text}"

    _profile_root(tmp_path)
    provider = MockProvider([
        ChatResponse(
            content="",
            tool_calls=[ToolCall(id="call_1", name="write", arguments={"text": "hello"})],
        ),
        ChatResponse(content="<think>checking</think>done"),
    ])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[write], review="careful")
    runner = dagent.Runner(workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    first = run(runner.run(agent, messages=user_messages("write hello")))
    assert first.review is not None

    async def collect() -> list[dagent.RunStreamChunk]:
        return [
            chunk
            async for chunk in runner.resume_stream(first.review.approve(), text_stream="content")
        ]

    chunks = run(collect())

    assert [chunk.text for chunk in chunks if chunk.text] == ["done"]
    assert chunks[-1].result is not None
    assert chunks[-1].result.output_text == "done"


def test_runner_stream_yields_typed_review_event(tmp_path) -> None:
    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        return f"wrote:{text}"

    _profile_root(tmp_path)
    provider = MockProvider([
        ChatResponse(
            content="",
            tool_calls=[ToolCall(id="call_1", name="write", arguments={"text": "hello"})],
        ),
    ])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[write], review="careful")
    runner = dagent.Runner(workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    async def collect() -> list[dagent.RunStreamEvent]:
        return [event async for event in runner.stream_events(agent, messages=user_messages("write hello"))]

    events = run(collect())

    review_events = [event for event in events if event.type == "review.required"]
    assert review_events
    assert review_events[-1].data.kind == "capability_review"
    assert review_events[-1].data.message == "Review capability call: tool.write"
    assert events[-1].type == "run.finished"
    assert events[-1].data.result.requires_review
    dumped = events[-1].data.result.model_dump(mode="json")
    assert dumped["state"]["pending_review"]["kind"] == "capability_review"


def test_runner_stream_chunk_exposes_review_without_event_type_branching(tmp_path) -> None:
    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        return f"wrote:{text}"

    _profile_root(tmp_path)
    provider = MockProvider([
        ChatResponse(
            content="",
            tool_calls=[ToolCall(id="call_1", name="write", arguments={"text": "hello"})],
        ),
    ])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[write], review="careful")
    runner = dagent.Runner(workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    async def collect() -> list[dagent.RunStreamChunk]:
        return [chunk async for chunk in runner.stream(agent, messages=user_messages("write hello"))]

    chunks = run(collect())

    review_chunks = [chunk for chunk in chunks if chunk.review is not None]
    assert review_chunks
    assert review_chunks[-1].review.kind == "capability_review"
    assert chunks[-1].result is not None
    assert chunks[-1].result.requires_review


def test_run_result_public_surface_uses_single_names(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="hello")])
    runner = dagent.Runner(workspace=tmp_path, provider=provider)

    result = run(runner.run(dagent.ToolAgent(profile="conversation"), messages=user_messages("hi")))

    assert result.output_text == "hello"
    assert result.run_id is not None
    assert result.state is not None
    assert result.requires_review is False
    for legacy_name in ("final_answer", "output", "task_id", "awaiting_review", "raw"):
        assert not hasattr(result, legacy_name)


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

    result = run(runner.run(agent, messages=user_messages("research X")))

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

    result = run(runner.run(agent, messages=user_messages("hi")))

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
        run(runner.run(agent, messages=user_messages("hi")))


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

    run(runner.run(agent, messages=user_messages("hi")))

    system_message = provider.requests[0]["messages"][0]["content"]
    assert "search" in system_message
    assert "write" not in system_message


def test_runner_agent_skills_filter_skill_tools_without_prompt_injection(tmp_path) -> None:
    skill_root = tmp_path / "skills"
    for category, name in (("writing", "brief"), ("research", "market")):
        skill_dir = skill_root / category / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} skill.\n---\nUse {name}.",
            encoding="utf-8",
        )
    provider = MockProvider([
        ChatResponse(tool_calls=[ToolCall(id="call_1", name="skills_list", arguments={})]),
        ChatResponse(content="done"),
    ])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[], skills=["writing/brief"])
    runner = dagent.Runner(workspace=tmp_path, provider=provider, skill_roots=[skill_root])

    result = run(runner.run(agent, messages=user_messages("list skills")))

    assert result.output_text == "done"
    assert [tool["function"]["name"] for tool in provider.requests[0]["tools"]] == [
        "skills_list",
        "skill_view",
    ]
    system_message = provider.requests[0]["messages"][0]["content"]
    assert "Use brief." not in system_message
    tool_content = provider.requests[1]["messages"][-1]["content"]
    assert "brief" in tool_content
    assert "market" not in tool_content


def test_runner_agent_empty_skills_disables_skill_tools(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="done")])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[], skills=[])
    runner = dagent.Runner(workspace=tmp_path, provider=provider)

    run(runner.run(agent, messages=user_messages("no tools")))

    assert provider.requests[0]["tools"] == []


def test_runner_default_agent_capabilities_exclude_registered_agent_capabilities(tmp_path) -> None:
    _profile_root(tmp_path, "writer")
    _profile_root(tmp_path, "conversation")
    provider = MockProvider([
        ChatResponse(content="drafted"),
        ChatResponse(content="hello"),
    ])
    writer = dagent.ToolAgent(profile="writer")
    dag = dagent.Dag("agent_flow")
    dag.add_node(dagent.Node("draft", target=writer, inputs={"prompt": "Draft the report."}))
    runner = dagent.Runner(workspace=tmp_path, provider=provider, profile_root=tmp_path / "profiles")

    run(runner.run(dag, workspace_root=tmp_path / "runs"))
    result = run(runner.run(dagent.ToolAgent(profile="conversation"), messages=user_messages("hi")))

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

    first = run(runner.run(agent, messages=user_messages("write hello")))
    assert first.requires_review
    assert first.review is not None

    resumed = run(runner.resume(first.review.approve()))

    assert resumed is not None
    assert resumed.output_text == "done"


def test_runner_resume_can_restore_pending_capability_gate_from_state(tmp_path) -> None:
    @dagent.tool(risk="medium")
    def write(text: str) -> str:
        return f"wrote:{text}"

    provider = MockProvider([
        ChatResponse(
            content="",
            tool_calls=[ToolCall(id="call_1", name="write", arguments={"text": "hello"})],
        ),
        ChatResponse(content="done"),
    ])
    agent = dagent.ToolAgent(profile="conversation", capabilities=[write], review="careful")
    first_runner = dagent.Runner(workspace=tmp_path, provider=provider, capabilities=[write])

    first = run(first_runner.run(agent, messages=user_messages("write hello")))
    saved_state = dagent.RunState.model_validate(first.state.model_dump(mode="json"))
    first_runner.close()

    second_runner = dagent.Runner(workspace=tmp_path, provider=provider, capabilities=[write])
    resumed = run(second_runner.resume(first.review.approve(), state=saved_state))

    assert resumed is not None
    assert resumed.output_text == "done"
    assert resumed.messages[-1]["content"] == "done"


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

    first = run(runner.run(agent, messages=user_messages("research X")))
    assert first.requires_review
    assert first.review is not None

    missing_dag = dagent.ReviewDecision(review_id=first.review.review_id, approved=True)
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
            "  directory: profiles",
        ]),
        encoding="utf-8",
    )

    runner = dagent.Runner.from_config(config, workspace=tmp_path)

    assert runner.profile_root == profiles
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
