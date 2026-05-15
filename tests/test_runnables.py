from dagent.capabilities import RunnableRegistry
from dagent.capabilities.providers import (
    AgentRunnableProvider,
    CustomToolRunnableProvider,
    FileRunnableProvider,
    MCPRunnableProvider,
    MemoryRunnableProvider,
    SkillRunnableProvider,
    ShellRunnableProvider,
    ToolRunnableProvider,
)
from dagent.harness_runtime import RunnableExecutor
from dagent.providers import ChatResponse, MockProvider
from dagent.schemas import Boundary, RunnableDefinition, RunnableInvocation
from dagent.tools.registry import ToolRegistry


def test_runnable_registry_rejects_duplicate_ids() -> None:
    registry = RunnableRegistry()
    definition = RunnableDefinition(
        id="tool.echo",
        name="echo",
        kind="tool",
        parameters={"type": "object"},
    )

    registry.register(definition)

    assert registry.get("tool.echo") == definition
    try:
        registry.register(definition)
    except ValueError as exc:
        assert "already registered" in str(exc)
    else:
        raise AssertionError("duplicate runnable id should fail")


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
    registry = RunnableRegistry()
    executor = RunnableExecutor(registry)
    ToolRunnableProvider(tools).register_into(registry, executor)

    result = executor.execute(
        RunnableInvocation(
            runnable_id="tool.echo",
            kind="tool",
            arguments={"text": "ok"},
            boundary=Boundary(mode="read_only", allowed_paths=["."]),
        )
    )

    assert registry.get("tool.echo").name == "echo"
    assert result.status == "completed"
    assert result.content == "echo:ok"
    assert result.kind == "tool"
    assert result.policy_decision["boundary_mode"] == "read_only"


def test_shell_provider_executes_allowlisted_command() -> None:
    registry = RunnableRegistry()
    executor = RunnableExecutor(registry)
    ShellRunnableProvider(
        commands={
            "say_hello": {
                "command": "python -c \"print('hello')\"",
                "description": "Print hello.",
            }
        }
    ).register_into(registry, executor)

    result = executor.execute(
        RunnableInvocation(
            runnable_id="shell.say_hello",
            kind="shell",
            arguments={},
            boundary=Boundary(mode="read_only", allowed_commands=["python"]),
        )
    )

    assert result.status == "completed"
    assert result.stdout.strip() == "hello"
    assert result.content.strip() == "hello"


def test_memory_and_file_providers_are_explicit_runnables(tmp_path) -> None:
    registry = RunnableRegistry()
    executor = RunnableExecutor(registry, workspace_root=tmp_path)
    MemoryRunnableProvider().register_into(registry, executor)
    FileRunnableProvider().register_into(registry, executor)

    write_memory = executor.execute(
        RunnableInvocation(
            runnable_id="memory.write",
            kind="memory",
            arguments={"scope": "session", "key": "color", "value": "blue"},
        )
    )
    search_memory = executor.execute(
        RunnableInvocation(
            runnable_id="memory.search",
            kind="memory",
            arguments={"scope": "session", "query": "color"},
        )
    )
    write_file = executor.execute(
        RunnableInvocation(
            runnable_id="file.write",
            kind="file",
            arguments={"path": "notes.txt", "content": "hello"},
            boundary=Boundary(mode="write_limited", allowed_paths=["."]),
        )
    )

    assert write_memory.status == "completed"
    assert "color=blue" in search_memory.content
    assert write_file.status == "completed"
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "hello"


def test_mcp_skill_custom_and_agent_providers_register_and_execute(tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "summarize"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarize text.\n---\nUse concise summaries.",
        encoding="utf-8",
    )
    provider = MockProvider([ChatResponse(content="agent:done")])
    registry = RunnableRegistry()
    executor = RunnableExecutor(registry, workspace_root=tmp_path)
    MCPRunnableProvider(
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
    ).register_into(registry, executor)
    SkillRunnableProvider(skill_roots=[tmp_path / "skills"]).register_into(registry, executor)
    CustomToolRunnableProvider(
        tools={
            "upper": {
                "description": "Uppercase text.",
                "handler": lambda text: str(text).upper(),
            }
        }
    ).register_into(registry, executor)
    AgentRunnableProvider(
        agents={
            "helper": {
                "system": "You are a helper.",
                "provider": provider,
            }
        }
    ).register_into(registry, executor)

    mcp_result = executor.execute(
        RunnableInvocation(runnable_id="mcp.mock.lookup", kind="mcp", arguments={"query": "x"})
    )
    skill_result = executor.execute(
        RunnableInvocation(runnable_id="skill.summarize", kind="skill", arguments={"input": "long text"})
    )
    custom_result = executor.execute(
        RunnableInvocation(runnable_id="custom_tool.upper", kind="custom_tool", arguments={"text": "hi"})
    )
    agent_result = executor.execute(
        RunnableInvocation(runnable_id="agent.helper", kind="agent", arguments={"prompt": "do it"})
    )

    assert mcp_result.content == "mcp:x"
    assert "Use concise summaries." in skill_result.content
    assert custom_result.content == "HI"
    assert agent_result.content == "agent:done"
    assert provider.requests[0]["messages"][0]["role"] == "system"
