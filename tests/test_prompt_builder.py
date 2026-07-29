from pathlib import Path

from dagent.profiles import AgentProfile
from dagent.state import PromptBuilder, PromptRequest
from dagent.schemas import CapabilityDefinition, PromptExtension


def test_prompt_builder_assembles_profile_and_dynamic_sections() -> None:
    profile = AgentProfile(
        name="dag_agent",
        content="DAGAgent soul\n\nDAGAgent agent instructions",
    )
    tool = CapabilityDefinition(
        id="tool.read_file",
        kind="tool",
        name="read",
        description="Read a file.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )

    messages = PromptBuilder().build(
        PromptRequest(
            profile=profile,
            task_content="Task {{ task_id }}: {{ user_request }}",
            tools=[tool],
            context="Project context.",
            variables={"task_id": "t1", "user_request": "hello"},
        )
    )

    assert messages[0]["role"] == "system"
    assert "DAGAgent soul" in messages[0]["content"]
    assert "DAGAgent agent instructions" in messages[0]["content"]
    assert "read (tool, id: tool.read_file): Read a file. Args: path." in messages[0]["content"]
    assert "Project context." in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "Task t1: hello"}


def test_prompt_builder_injects_resolved_workspace_runtime_context(
    tmp_path: Path,
) -> None:
    profile = AgentProfile(name="conversation", content="Stay concise.")
    workspace = tmp_path / "project" / ".." / "workspace"

    message = PromptBuilder().build_system_message(
        PromptRequest(
            profile=profile,
            task_content="",
            workspace_path=workspace,
        )
    )

    assert message["content"] == (
        "Stay concise.\n\n"
        "## Runtime Context\n"
        f"- Workspace root: {workspace.resolve()}\n"
        "- Resolve relative file paths from this workspace root."
    )
    assert profile.content == "Stay concise."


def test_prompt_builder_orders_and_filters_host_extensions(
    tmp_path: Path,
) -> None:
    profile = AgentProfile(name="conversation", content="PROFILE")
    tool = CapabilityDefinition(
        id="tool.read_file",
        kind="tool",
        description="Read a file.",
    )
    builder = PromptBuilder()
    baseline = builder.build_system_message(
        PromptRequest(
            profile=profile,
            task_content="",
            workspace_path=tmp_path,
            tools=[tool],
            context="DYNAMIC_CONTEXT",
        )
    )
    explicit_empty = builder.build_system_message(
        PromptRequest(
            profile=profile,
            task_content="",
            workspace_path=tmp_path,
            tools=[tool],
            context="DYNAMIC_CONTEXT",
            prompt_extensions=(),
        )
    )
    extended = builder.build_system_message(
        PromptRequest(
            profile=profile,
            task_content="",
            workspace_path=tmp_path,
            tools=[tool],
            context="DYNAMIC_CONTEXT",
            prompt_extensions=(
                PromptExtension(
                    id="host.z",
                    content="EXTENSION_Z",
                    targets=["tool_agent"],
                ),
                PromptExtension(
                    id="host.a",
                    content="EXTENSION_A {{ host_does_not_render_here }}",
                    targets=["tool_agent"],
                ),
                PromptExtension(
                    id="host.dag",
                    content="DAG_ONLY",
                    targets=["dag_planner"],
                ),
            ),
            prompt_target="tool_agent",
        )
    )

    assert explicit_empty == baseline
    content = extended["content"]
    assert "DAG_ONLY" not in content
    assert "{{ host_does_not_render_here }}" in content
    assert (
        content.index("PROFILE")
        < content.index("## Runtime Context")
        < content.index("Host Prompt Extension: host.a")
        < content.index("Host Prompt Extension: host.z")
        < content.index("## Available Tools")
        < content.index("DYNAMIC_CONTEXT")
    )


def test_prompt_builder_renders_user_message_template() -> None:
    builder = PromptBuilder()
    message = builder.build_user_message(
        "Task {{ task_id }}: {{ user_request }}",
        {"task_id": "t1", "user_request": "current question"},
    )

    assert message == {"role": "user", "content": "Task t1: current question"}


def test_prompt_builder_keeps_template_control_blocks_literal() -> None:
    builder = PromptBuilder()
    template = "Request:\n{% if context %}Context:\n{{ context }}\n\n{% endif %}Done."

    message = builder.build_user_message(template, {"context": "details"})

    assert message["content"] == "Request:\n{% if context %}Context:\ndetails\n\n{% endif %}Done."


def test_prompt_builder_keeps_jinja_expressions_literal() -> None:
    builder = PromptBuilder()
    template = "Literal expression: {{ 7 * 6 }}"

    message = builder.build_user_message(template, {})

    assert message["content"] == template


def test_prompt_builder_does_not_reparse_substituted_values() -> None:
    builder = PromptBuilder()
    message = builder.build_user_message(
        "{{ payload }}",
        {"payload": '{"a": 1} {% raw %} {{ not_a_var }}'},
    )

    assert message["content"] == '{"a": 1} {% raw %} {{ not_a_var }}'
