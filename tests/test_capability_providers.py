import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from dagent.capabilities import CapabilityCatalog, CapabilityToolAdapter, CapabilityToolset
from dagent.capabilities.cancellation import run_cancellation_context
from dagent.capabilities.mcp import MCPCapabilityProvider
from dagent.capabilities.providers import (
    AgentCapabilityProvider,
    MemoryCapabilityProvider,
    ToolCapabilityProvider,
)
from dagent.capabilities.skills import SkillsCapabilityProvider
from dagent.capabilities.tools.file_tools import create_file_tool_registry
import dagent.capabilities as capabilities_module
import dagent.capabilities.providers as providers_module
import dagent.capabilities.skills as skills_module
from dagent.harness_runtime import CapabilityExecutor
from dagent.harness_runtime.capability_executor import CapabilityExecutionContext
from dagent.profiles import AgentProfile
from dagent.providers import ChatResponse, MockProvider
from dagent.schemas import Boundary, CapabilityDefinition, CapabilityInvocation, CapabilityResult, DAGNode
from dagent.capabilities.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def test_capability_catalog_rejects_duplicate_ids() -> None:
    registry = CapabilityCatalog()
    definition = CapabilityDefinition(
        id="tool.echo",
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
            boundary=Boundary(allowed_paths=["."]),
        )
    ))

    assert registry.get("tool.echo") is not None
    assert result.status == "completed"
    assert result.content == "echo:ok"
    assert result.kind == "tool"
    assert result.policy_decision == {"allowed_paths": ["."]}


def test_tool_provider_passes_run_cancellation_event_to_cooperative_tool() -> None:
    received_events: list[threading.Event | None] = []

    def cooperative_tool(*, _dagent_cancel_event=None):
        received_events.append(_dagent_cancel_event)
        return "done"

    tools = ToolRegistry()
    tools.register(
        name="cooperative",
        handler=cooperative_tool,
        action="read",
        parameters={"type": "object", "properties": {}},
    )
    registry = CapabilityCatalog()
    executor = CapabilityExecutor(registry)
    ToolCapabilityProvider(tools).register_into(registry)
    cancellation_event = threading.Event()

    with run_cancellation_context(cancellation_event):
        result = run(executor.execute(CapabilityInvocation(
            capability_id="tool.cooperative",
            kind="tool",
        )))

    assert result.status == "completed"
    assert received_events == [cancellation_event]


def test_capability_executor_runs_tools_in_context_workspace(tmp_path) -> None:
    catalog_root = tmp_path / "catalog"
    run_workspace = tmp_path / "runs" / "run_1"
    run_workspace.mkdir(parents=True)
    catalog = CapabilityCatalog(workspace_root=catalog_root)
    executor = CapabilityExecutor(catalog)
    ToolCapabilityProvider(create_file_tool_registry()).register_into(catalog)
    invocation = CapabilityInvocation(
        capability_id="tool.write_file",
        kind="tool",
        arguments={"path": "notes/output.txt", "content": "hello"},
        boundary=Boundary(allowed_paths=["."]),
    )

    result = run(
        executor.execute(
            invocation,
            context=CapabilityExecutionContext(
                task_id="run_1",
                workspace_path=run_workspace,
            ),
        )
    )

    assert result.error is None
    assert (run_workspace / "notes" / "output.txt").read_text(encoding="utf-8") == "hello"
    assert not (catalog_root / "notes" / "output.txt").exists()


def test_tool_provider_encodes_plain_tuple_results_like_sdk_tools() -> None:
    tools = ToolRegistry()
    tools.register(
        name="pair",
        handler=lambda: ("content", ["entry"]),
        action="read",
        parameters={"type": "object"},
    )
    registry = CapabilityCatalog()
    executor = CapabilityExecutor(registry)
    ToolCapabilityProvider(tools).register_into(registry)

    result = run(executor.execute(
        CapabilityInvocation(
            capability_id="tool.pair",
            kind="tool",
            arguments={},
            boundary=Boundary(allowed_paths=["."]),
        )
    ))

    assert result.status == "completed"
    assert result.content == '["content", ["entry"]]'
    assert result.value == ["content", ["entry"]]


def test_memory_and_file_tool_capabilities_are_explicit(tmp_path) -> None:
    registry = CapabilityCatalog(workspace_root=tmp_path)
    executor = CapabilityExecutor(registry)
    MemoryCapabilityProvider().register_into(registry)
    ToolCapabilityProvider(create_file_tool_registry()).register_into(registry)

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
            capability_id="tool.write_file",
            kind="tool",
            arguments={"path": "notes.txt", "content": "hello"},
            boundary=Boundary(allowed_paths=["."]),
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
    node = DAGNode(id="agent_node", payload=dict(type="capability", invocation=invocation))
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
        CapabilityDefinition(id="agent.contextual", kind="agent"),
        handler,
        supports_context=True,
    )

    assert not hasattr(executor, "execute_async")
    result = run(executor.execute(invocation, context=context))

    assert result.status == "completed"
    assert result.content == "node:agent_node"
    assert seen == {"node_id": "agent_node"}


def test_capability_provider_legacy_import_shims_are_removed() -> None:
    assert not hasattr(providers_module, "MCPCapabilityProvider")
    assert not hasattr(providers_module, "SkillsCapabilityProvider")
    assert not hasattr(providers_module, "SkillCapabilityProvider")
    assert not hasattr(skills_module, "SkillCapabilityProvider")
    assert not hasattr(capabilities_module, "SkillCapabilityProvider")


def test_template_capability_handler_is_not_exposed_from_sdk_providers() -> None:
    assert not hasattr(providers_module, "template_capability_handler")


def test_mcp_skill_and_agent_providers_register_and_execute(tmp_path) -> None:
    skill_dir = tmp_path / "skills" / "summarize"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarize text.\n---\nUse concise summaries.",
        encoding="utf-8",
    )
    provider = MockProvider([ChatResponse(content="agent:done")])
    registry = CapabilityCatalog()
    executor = CapabilityExecutor(registry)

    class FakeMCPManager:
        available = True

        def start(self) -> None:
            return None

        def discovered_tools(self):
            return {
                "mock": [
                    SimpleNamespace(
                        name="lookup",
                        description="Lookup a value.",
                        inputSchema={"type": "object"},
                    )
                ]
            }

        def call_tool_blocking(self, server_name, tool_name, arguments, timeout):
            return SimpleNamespace(
                isError=False,
                content=[SimpleNamespace(type="text", text=f"mcp:{arguments['query']}")],
                structuredContent=None,
            )

    MCPCapabilityProvider(
        servers={"mock": {"command": "fake"}},
        manager=FakeMCPManager(),
    ).register_into(registry)
    SkillsCapabilityProvider(skill_roots=[tmp_path / "skills"]).register_into(registry)
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
        CapabilityInvocation(capability_id="skill.view", kind="skill", arguments={"name": "summarize"})
    ))
    agent_result = run(executor.execute(
        CapabilityInvocation(capability_id="agent.helper", kind="agent", arguments={"prompt": "do it"})
    ))

    assert mcp_result.content == "mcp:x"
    assert "Use concise summaries." in json.loads(skill_result.content)["content"]
    assert agent_result.content == "agent:done"
    assert registry.get("skill.list") is not None
    assert registry.get("skill.view") is not None
    agent_definition = registry.get("agent.helper")
    assert agent_definition is not None
    assert agent_definition.parameters["properties"]["prompt"]["default"] == ""
    assert agent_definition.parameters["properties"]["max_steps"]["default"] == 8
    assert provider.requests[0]["messages"][0]["role"] == "system"


def test_agent_provider_rejects_nested_agent_capability_adapter(tmp_path) -> None:
    provider = MockProvider([ChatResponse(content="unused")])
    registry = CapabilityCatalog()
    executor = CapabilityExecutor(registry)
    registry.register(
        CapabilityDefinition(id="agent.child", kind="agent"),
        lambda invocation: CapabilityResult(
            invocation_id=invocation.invocation_id,
            capability_id=invocation.capability_id,
            kind=invocation.kind,
            status="completed",
            content="child",
        ),
    )
    tool_adapter = CapabilityToolAdapter(
        registry,
        toolsets=[CapabilityToolset("builtin", ("agent.child",))],
    )
    with pytest.raises(ValueError, match="cannot expose subagents"):
        AgentCapabilityProvider(
            agents={
                "helper": {
                    "provider": provider,
                    "profile": AgentProfile(name="helper", content="You are a helper."),
                    "capability_executor": executor,
                    "tool_adapter": tool_adapter,
                    "enabled_toolsets": ("builtin",),
                }
            }
        ).register_into(registry)


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
        content="You are a helper agent.",
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
        payload=dict(
            type="capability",
            invocation=CapabilityInvocation(
                capability_id="agent.helper",
                kind="agent",
                arguments={"prompt": "Draft the requirements. Keep it concise."},
            ),
        ),
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

    first = run(executor.execute(node_a.payload.invocation, context=context_a))
    second = run(executor.execute(node_a.payload.invocation, context=context_a))

    node_b = node_a.model_copy(
        update={
            "id": "write_tests",
            "title": "Write tests",
            "payload": node_a.payload.model_copy(
                update={
                    "invocation": CapabilityInvocation(
                        capability_id="agent.helper",
                        kind="agent",
                        arguments={"prompt": "Draft test cases. Cover failures."},
                    ),
                },
                deep=True,
            ),
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
    other = run(executor.execute(node_b.payload.invocation, context=context_b))

    assert first.content == "first"
    assert second.content == "second"
    assert other.content == "other"
    assert [message["role"] for message in provider.requests[0]["messages"]] == ["system", "user"]
    assert [message["role"] for message in provider.requests[1]["messages"]] == ["system", "user", "assistant"]
    assert [message["role"] for message in provider.requests[2]["messages"]] == ["system", "user"]

    first_system = provider.requests[0]["messages"][0]["content"]
    first_user = provider.requests[0]["messages"][1]["content"]
    assert "You are a helper agent." in first_system
    assert str(tmp_path) in first_system
    assert "source_doc" in first_system
    assert "requirements_doc" in first_system
    assert "Write requirements" in first_user
    assert "Draft the requirements. Keep it concise." in first_user
    assert "source_doc" not in first_user


def test_agent_provider_resets_node_session_when_invocation_arguments_change(tmp_path) -> None:
    provider = MockProvider([
        ChatResponse(content="first"),
        ChatResponse(content="second"),
    ])
    registry = CapabilityCatalog(workspace_root=tmp_path)
    executor = CapabilityExecutor(registry)
    tool_adapter = CapabilityToolAdapter(
        registry,
        toolsets=[CapabilityToolset("builtin", ())],
    )
    AgentCapabilityProvider(
        agents={
            "helper": {
                "provider": provider,
                "profile": AgentProfile(name="helper", content="You are a helper."),
                "capability_executor": executor,
                "tool_adapter": tool_adapter,
                "enabled_toolsets": ("builtin",),
            }
        }
    ).register_into(registry)
    node = DAGNode(
        id="ask",
        payload=dict(
            type="capability",
            invocation=CapabilityInvocation(
                capability_id="agent.helper",
                kind="agent",
                arguments={"prompt": "First prompt."},
            ),
        ),
    )
    context = CapabilityExecutionContext(task_id="run_1", node=node, workspace_path=tmp_path)

    first = run(executor.execute(node.payload.invocation, context=context))
    second_invocation = CapabilityInvocation(
        capability_id="agent.helper",
        kind="agent",
        arguments={"prompt": "Second prompt."},
    )
    second = run(executor.execute(second_invocation, context=context))

    assert first.content == "first"
    assert second.content == "second"
    first_user = provider.requests[0]["messages"][1]["content"]
    second_user = provider.requests[1]["messages"][1]["content"]
    assert "First prompt." in first_user
    assert "Second prompt." in second_user
    assert [message["role"] for message in provider.requests[1]["messages"]] == ["system", "user"]
