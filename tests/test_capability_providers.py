import asyncio

from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.capabilities.providers import (
    AgentCapabilityProvider,
    CustomToolCapabilityProvider,
    FileCapabilityProvider,
    MCPCapabilityProvider,
    MemoryCapabilityProvider,
    SkillCapabilityProvider,
    ShellCapabilityProvider,
    ToolCapabilityProvider,
)
from dagent.harness_runtime import CapabilityExecutor
from dagent.harness_runtime.capability_executor import CapabilityExecutionContext
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider
from dagent.schemas import Boundary, CapabilityDefinition, CapabilityInvocation, CapabilityResult, DAGNode
from dagent.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def test_capability_catalog_rejects_duplicate_ids() -> None:
    registry = CapabilityCatalog()
    definition = CapabilityDefinition(
        id="tool.echo",
        name="echo",
        kind="tool",
        parameters={"type": "object"},
    )

    registry.register(definition, lambda invocation: None)

    assert registry.get("tool.echo") == definition
    try:
        registry.register(definition, lambda invocation: None)
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("duplicate capability id should fail")


def test_tool_provider_exposes_and_executes_existing_tools() -> None:
    tools = ToolRegistry()
    tools.register(
        name="echo",
        handler=lambda text: f"echo:{text}",
        action="read",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )
    registry = CapabilityCatalog()
    executor = CapabilityExecutor(registry)
    ToolCapabilityProvider(tools).register_into(registry)

    result = run(executor.execute(
        CapabilityInvocation(
            capability_id="tool.echo",
            kind="tool",
            arguments={"text": "ok"},
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
        )
    ))

    assert registry.get("tool.echo").name == "echo"
    assert result.status == "completed"
    assert result.content == "echo:ok"
    assert result.kind == "tool"
    assert result.policy_decision["boundary_mode"] == "read_only"


def test_shell_provider_executes_allowlisted_command() -> None:
    registry = CapabilityCatalog()
    executor = CapabilityExecutor(registry)
    ShellCapabilityProvider(
        commands={
            "say_hello": {
                "command": "python -c \"print('hello')\"",
                "description": "Print hello.",
            }
        }
    ).register_into(registry)

    result = run(executor.execute(
        CapabilityInvocation(
            capability_id="shell.say_hello",
            kind="shell",
            arguments={},
            boundary=Boundary(mode="read_only", allowed_commands=["python"]),
        )
    ))

    assert result.status == "completed"
    assert result.stdout.strip() == "hello"
    assert result.content.strip() == "hello"


def test_memory_and_file_providers_are_explicit_capabilities(tmp_path) -> None:
    registry = CapabilityCatalog(workspace_root=tmp_path)
    executor = CapabilityExecutor(registry)
    MemoryCapabilityProvider().register_into(registry)
    FileCapabilityProvider().register_into(registry)

    write_memory = run(executor.execute(
        CapabilityInvocation(
            capability_id="memory.write",
            kind="memory",
            arguments={"scope": "session", "key": "color", "value": "blue"},
        )
    ))
    search_memory = run(executor.execute(
        CapabilityInvocation(
            capability_id="memory.search",
            kind="memory",
            arguments={"scope": "session", "query": "color"},
        )
    ))
    write_file = run(executor.execute(
        CapabilityInvocation(
            capability_id="file.write",
            kind="file",
            arguments={"path": "notes.txt", "content": "hello"},
            boundary=Boundary(mode="write_limited", allowed_paths=["."]),
        )
    ))

    assert write_memory.status == "completed"
    assert "color=blue" in search_memory.content
    assert write_file.status == "completed"
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "hello"


def test_capability_executor_passes_context_to_async_handler(tmp_path) -> None:
    registry = CapabilityCatalog()
    executor = CapabilityExecutor(registry)
    invocation = CapabilityInvocation(capability_id="agent.contextual", kind="agent")
    node = DAGNode(id="agent_node", invocation=invocation)
    context = CapabilityExecutionContext(
        task_id="task_1",
        dag_id="dag_1",
        node=node,
        workspace_path=tmp_path,
    )
    seen: dict[str, str] = {}

    async def handler(
        invocation: CapabilityInvocation,
        *,
        context: CapabilityExecutionContext | None,
        callbacks,
    ) -> CapabilityResult:
        assert context is not None
        seen["node_id"] = context.node.id
        return CapabilityResult(
            invocation_id=invocation.invocation_id,
            capability_id=invocation.capability_id,
            kind=invocation.kind,
            status="completed",
            content=f"node:{context.node.id}",
        )

    registry.register(
        CapabilityDefinition(id="agent.contextual", name="contextual", kind="agent"),
        handler,
        supports_context=True,
    )

    assert not hasattr(executor, "execute_async")
    result = run(executor.execute(invocation, context=context))

    assert result.status == "completed"
    assert result.content == "node:agent_node"
    assert seen == {"node_id": "agent_node"}


def test_mcp_skill_custom_and_agent_providers_register_and_execute(tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "summarize"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarize text.\n---\nUse concise summaries.",
        encoding="utf-8",
    )
    provider = MockProvider([ChatResponse(content="agent:done")])
    registry = CapabilityCatalog()
    executor = CapabilityExecutor(registry)
    MCPCapabilityProvider(
        servers={
            "mock": {
                "tools": {
                    "lookup": {
                        "description": "Lookup a value.",
                        "handler": lambda query: f"mcp:{query}",
                    }
                }
            }
        }
    ).register_into(registry)
    SkillCapabilityProvider(skill_roots=[tmp_path / "skills"]).register_into(registry)
    CustomToolCapabilityProvider(
        tools={
            "upper": {
                "description": "Uppercase text.",
                "handler": lambda text: str(text).upper(),
            }
        }
    ).register_into(registry)
    AgentCapabilityProvider(
        agents={
            "helper": {
                "system": "You are a helper.",
                "provider": provider,
            }
        }
    ).register_into(registry)

    mcp_result = run(executor.execute(
        CapabilityInvocation(capability_id="mcp.mock.lookup", kind="mcp", arguments={"query": "x"})
    ))
    skill_result = run(executor.execute(
        CapabilityInvocation(capability_id="skill.summarize", kind="skill", arguments={"input": "long text"})
    ))
    custom_result = run(executor.execute(
        CapabilityInvocation(capability_id="custom_tool.upper", kind="custom_tool", arguments={"text": "hi"})
    ))
    agent_result = run(executor.execute(
        CapabilityInvocation(capability_id="agent.helper", kind="agent", arguments={"prompt": "do it"})
    ))

    assert mcp_result.content == "mcp:x"
    assert "Use concise summaries." in skill_result.content
    assert custom_result.content == "HI"
    assert agent_result.content == "agent:done"
    assert provider.requests[0]["messages"][0]["role"] == "system"


def test_agent_provider_uses_scoped_node_messages(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(content="first"),
        ChatResponse(content="second"),
        ChatResponse(content="other"),
    ])
    registry = CapabilityCatalog(workspace_root=tmp_path)
    executor = CapabilityExecutor(registry)
    tool_adapter = CapabilityToolAdapter(
        registry,
        toolsets=[CapabilityToolset("builtin", ())],
    )
    profile = AgentProfile(
        name="helper",
        role="agent",
        layers=["agent.md"],
        layer_contents={"agent.md": "You are a helper agent."},
        memory="Remember project conventions.",
    )
    AgentCapabilityProvider(
        agents={
            "helper": {
                "provider": provider,
                "profile": profile,
                "capability_executor": executor,
                "tool_adapter": tool_adapter,
                "enabled_toolsets": ("builtin",),
            }
        }
    ).register_into(registry)

    node_a = DAGNode(
        id="write_requirements",
        title="Write requirements",
        goal="Draft the requirements.",
        instructions="Keep it concise.",
        invocation=CapabilityInvocation(capability_id="agent.helper", kind="agent"),
    )
    context_a = CapabilityExecutionContext(
        task_id="run_1",
        dag_id="dag_1",
        spec_id="spec_1",
        node=node_a,
        workspace_path=tmp_path,
        input_artifacts={"source_doc": [tmp_path / "inputs" / "source.md"]},
        output_artifacts={"requirements_doc": [tmp_path / "outputs" / "requirements.md"]},
    )

    first = run(executor.execute(node_a.invocation, context=context_a))
    second = run(executor.execute(node_a.invocation, context=context_a))

    node_b = node_a.model_copy(
        update={
            "id": "write_tests",
            "title": "Write tests",
            "goal": "Draft test cases.",
            "instructions": "Cover failures.",
            "invocation": CapabilityInvocation(capability_id="agent.helper", kind="agent"),
        },
        deep=True,
    )
    context_b = CapabilityExecutionContext(
        task_id="run_1",
        dag_id="dag_1",
        spec_id="spec_1",
        node=node_b,
        workspace_path=tmp_path,
    )
    other = run(executor.execute(node_b.invocation, context=context_b))

    assert first.content == "first"
    assert second.content == "second"
    assert other.content == "other"
    assert [message["role"] for message in provider.requests[0]["messages"]] == ["system", "user"]
    assert [message["role"] for message in provider.requests[1]["messages"]] == ["system", "user", "assistant"]
    assert [message["role"] for message in provider.requests[2]["messages"]] == ["system", "user"]

    first_system = provider.requests[0]["messages"][0]["content"]
    first_user = provider.requests[0]["messages"][1]["content"]
    assert "You are a helper agent." in first_system
    assert "Remember project conventions." in first_system
    assert str(tmp_path) in first_system
    assert "source_doc" in first_system
    assert "requirements_doc" in first_system
    assert "Write requirements" in first_user
    assert "Draft the requirements." in first_user
    assert "Keep it concise." in first_user
    assert "source_doc" not in first_user
