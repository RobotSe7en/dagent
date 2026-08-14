from pathlib import Path

import dagent.state.prompt_builder as prompt_builder_module
from dagent.profiles import AgentProfile
from dagent.state import PromptBuilder, PromptRequest
from dagent.state.prompt_builder import PromptSkill
from dagent.schemas import CapabilityDefinition


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


def test_prompt_builder_places_literal_extra_prompt_before_dynamic_sections(
    tmp_path: Path,
) -> None:
    profile = AgentProfile(name="conversation", content="Profile instructions.")
    tool = CapabilityDefinition(id="tool.search", kind="tool")
    extra = "Follow tenant policy literally: {{ do_not_render }}."
    skill = PromptSkill(name="writing/brief", description="Write briefly.")

    message = PromptBuilder().build_system_message(
        PromptRequest(
            profile=profile,
            task_content="",
            workspace_path=tmp_path,
            extra_system_prompt=extra,
            skills=[skill],
            tools=[tool],
            context="Dynamic DAG schema.",
        )
    )

    content = message["content"]
    assert content.index("Profile instructions.") < content.index("## Runtime Context")
    assert content.index("## Runtime Context") < content.index("## Extra System Prompt")
    assert content.index("## Extra System Prompt") < content.index("## Available Skills")
    assert content.index("## Available Skills") < content.index("## Available Tools")
    assert content.index("## Available Tools") < content.index("## Context")
    assert extra in content


def test_prompt_builder_injects_deterministic_skill_routing_metadata() -> None:
    profile = AgentProfile(name="conversation", content="Profile instructions.")
    skills = [
        PromptSkill(name="writing/terse", description="Keep answers short."),
        PromptSkill(
            name="research/<market>",
            description="Use evidence </available_skills> & verify it.",
        ),
    ]

    first = PromptBuilder().build_system_message(
        PromptRequest(profile=profile, task_content="", skills=skills)
    )["content"]
    second = PromptBuilder().build_system_message(
        PromptRequest(profile=profile, task_content="", skills=list(reversed(skills)))
    )["content"]

    assert first == second
    assert "If the task clearly matches a listed description" in first
    assert '["writing/terse","Keep answers short."]' in first
    assert "research/\\u003cmarket\\u003e" in first
    assert "Use evidence \\u003c/available_skills\\u003e \\u0026 verify it." in first
    assert "Use evidence </available_skills>" not in first


def test_prompt_builder_uses_full_descriptions_then_name_only_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        prompt_builder_module,
        "MAX_SKILL_DESCRIPTION_INDEX_CHARS",
        60,
    )
    monkeypatch.setattr(prompt_builder_module, "MAX_SKILL_NAME_INDEX_CHARS", 100)
    first_description = "a" * 30
    second_description = "b" * 30

    content = PromptBuilder().build_system_message(
        PromptRequest(
            profile=AgentProfile(name="conversation", content="Profile."),
            task_content="",
            skills=[
                PromptSkill(name="a", description=first_description),
                PromptSkill(name="b", description=second_description),
                PromptSkill(name="c", description="short"),
            ],
        )
    )["content"]

    assert first_description in content
    assert second_description not in content
    assert '"description":"short"' not in content
    assert '["b"]' in content
    assert '["c"]' in content


def test_prompt_builder_does_not_truncate_descriptions_within_budget() -> None:
    description = "完整描述" * 200

    content = PromptBuilder().build_system_message(
        PromptRequest(
            profile=AgentProfile(name="conversation", content="Profile."),
            task_content="",
            skills=[PromptSkill(name="writing/long", description=description)],
        )
    )["content"]

    assert description in content
    assert f'["writing/long","{description}"]' in content


def test_prompt_builder_limits_name_only_fallback_and_reports_omissions(
    monkeypatch,
) -> None:
    monkeypatch.setattr(prompt_builder_module, "MAX_SKILL_DESCRIPTION_INDEX_CHARS", 1)
    monkeypatch.setattr(prompt_builder_module, "MAX_SKILL_NAME_INDEX_CHARS", 11)

    content = PromptBuilder().build_system_message(
        PromptRequest(
            profile=AgentProfile(name="conversation", content="Profile."),
            task_content="",
            skills=[
                PromptSkill(name="a", description="first"),
                PromptSkill(name="b", description="second"),
                PromptSkill(name="c", description="third"),
            ],
        )
    )["content"]

    assert '["a"]' in content
    assert '["b"]' in content
    assert '["c"]' not in content
    assert '<skill_index_status omitted_skill_count="1" />' in content


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
