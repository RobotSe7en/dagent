from dagent.profiles import AgentProfile
from dagent.state import PromptBuilder, PromptRequest
from dagent.schemas import CapabilityDefinition


def test_prompt_builder_assembles_profile_and_dynamic_sections() -> None:
    profile = AgentProfile(
        name="dag_agent",
        content="DAGAgent soul\n\nDAGAgent agent instructions",
    )
    tool = CapabilityDefinition(
        id="tool.read_file",
        name="read_file",
        kind="tool",
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
    assert "read_file (tool, id: tool.read_file): Read a file. Args: path." in messages[0]["content"]
    assert "Project context." in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "Task t1: hello"}


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
