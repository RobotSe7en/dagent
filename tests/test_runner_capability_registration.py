import asyncio
import json
from types import SimpleNamespace

import pytest

import dagent
from dagent.providers import MockProvider
from dagent.schemas import CapabilityInvocation


def run(coro):
    return asyncio.run(coro)


class FakeMCPManager:
    available = True

    def __init__(self) -> None:
        self.started = False
        self.shutdown_calls = 0
        self.last_errors: dict[str, str] = {}

    def start(self) -> None:
        self.started = True

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def discovered_tools(self):
        return {
            "mock-server": [
                SimpleNamespace(
                    name="lookup",
                    description="Lookup a value.",
                    inputSchema={"properties": {"query": {"type": "string"}}, "required": ["query"]},
                )
            ]
        }

    def call_tool_blocking(self, server_name, tool_name, arguments, timeout):
        return SimpleNamespace(
            isError=False,
            content=[SimpleNamespace(type="text", text="found:x")],
            structuredContent=None,
        )


def _runner(tmp_path) -> dagent.Runner:
    return dagent.Runner(workspace=tmp_path, provider=MockProvider([]))


def test_add_skill_root_makes_skill_discoverable_at_runtime(tmp_path) -> None:
    runner = _runner(tmp_path)
    skill_dir = tmp_path / "extra" / "writing" / "summarize"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarize carefully.\n---\nUse concise summaries.",
        encoding="utf-8",
    )

    runner.add_skill_root(tmp_path / "extra")

    names = {skill.name for skill in runner.skill_store.list()}
    assert "summarize" in names

    result = run(runner.runtime.capability_executor.execute(
        CapabilityInvocation(capability_id="skill.list", kind="skill")
    ))
    payload = json.loads(result.content)
    assert any(skill["name"] == "summarize" for skill in payload["skills"])

    definition = runner.runtime.capability_catalog.get("skill.list")
    assert str(tmp_path / "extra") in definition.config["roots"]
    runner.close()


def test_add_skill_root_is_idempotent(tmp_path) -> None:
    runner = _runner(tmp_path)
    before = len(runner.skill_store.roots)
    runner.add_skill_root(tmp_path / "extra")
    runner.add_skill_root(tmp_path / "extra")
    assert len(runner.skill_store.roots) == before + 1
    runner.close()


def test_add_mcp_server_registers_tools_and_makes_them_visible(tmp_path) -> None:
    runner = _runner(tmp_path)
    manager = FakeMCPManager()

    definitions = runner.add_mcp_server("mock-server", {"command": "fake"}, manager=manager)

    assert manager.started is True
    assert [definition.id for definition in definitions] == ["mcp.mock_server.lookup"]
    assert any(definition.id == "mcp.mock_server.lookup" for definition in runner.capabilities)

    result = run(runner.runtime.capability_executor.execute(
        CapabilityInvocation(capability_id="mcp.mock_server.lookup", kind="mcp", arguments={"query": "x"})
    ))
    assert result.status == "completed"
    assert result.content == "found:x"
    runner.close()


def test_add_mcp_server_requires_mcp_sdk_without_manager(tmp_path) -> None:
    runner = _runner(tmp_path)
    # The dev test environment does not install the optional MCP SDK.
    with pytest.raises(RuntimeError, match="MCP SDK is not installed"):
        runner.add_mcp_server("mock-server", {"command": "fake"})
    runner.close()


def test_add_mcp_server_raises_on_connect_failure(tmp_path) -> None:
    runner = _runner(tmp_path)
    manager = FakeMCPManager()
    manager.last_errors = {"mock-server": "boom"}
    manager.discovered_tools = lambda: {}

    with pytest.raises(RuntimeError, match="failed to connect: boom"):
        runner.add_mcp_server("mock-server", {"command": "fake"}, manager=manager)
    assert manager.shutdown_calls == 1
    runner.close()


def test_add_mcp_server_rolls_back_partial_registration_on_error(tmp_path) -> None:
    runner = _runner(tmp_path)

    # A tool whose LLM function name collides with one of the MCP server's tools.
    @dagent.tool(id="tool.collide", name="mcp_mock_server__dup")
    def collide() -> str:
        return "x"

    runner.add_tool(collide)

    class TwoToolManager(FakeMCPManager):
        def discovered_tools(self):
            return {
                "mock-server": [
                    SimpleNamespace(name="good", description="", inputSchema={"properties": {}}),
                    SimpleNamespace(name="dup", description="", inputSchema={"properties": {}}),
                ]
            }

    manager = TwoToolManager()

    with pytest.raises(RuntimeError, match="collides"):
        runner.add_mcp_server("mock-server", {"command": "fake"}, manager=manager)

    catalog = runner.runtime.capability_catalog
    # The successfully-registered "good" tool must be rolled back...
    assert catalog.get("mcp.mock_server.good") is None
    # ...the pre-existing colliding tool must survive...
    assert catalog.get("tool.collide") is not None
    # ...and the manager must be shut down.
    assert manager.shutdown_calls == 1
    runner.close()
