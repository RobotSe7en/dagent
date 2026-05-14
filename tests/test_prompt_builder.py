from dagent.profiles import AgentProfile
from dagent.state import PromptBuilder, PromptRequest
from dagent.tools.registry import Tool


def test_prompt_builder_assembles_profile_and_dynamic_sections() -> None:
    profile = AgentProfile(
        name="dag_agent",
        role="dag_agent",
        layers=["soul.md", "agent.md"],
        layer_contents={
            "soul.md": "DAGAgent soul",
            "agent.md": "DAGAgent agent instructions",
        },
    )
    tool = Tool(
        name="read_file",
        handler=lambda: "",
        action="read",
        description="Read a file.",
    )

    messages = PromptBuilder().build(
        PromptRequest(
            profile=profile,
            task_content="Task {{ task_id }}: {{ user_request }}",
            tools=[tool],
            skills=["code_review"],
            memory="Remember narrow boundaries.",
            context="Project context.",
            variables={"task_id": "t1", "user_request": "hello"},
        )
    )

    assert messages[0]["role"] == "system"
    assert "DAGAgent soul" in messages[0]["content"]
    assert "DAGAgent agent instructions" in messages[0]["content"]
    assert "read_file: Read a file." in messages[0]["content"]
    assert "code_review" in messages[0]["content"]
    assert "Remember narrow boundaries." in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "Task t1: hello"}


def test_prompt_builder_renders_user_message_template() -> None:
    builder = PromptBuilder()
    message = builder.build_user_message(
        "Task {{ task_id }}: {{ user_request }}",
        {"task_id": "t1", "user_request": "current question"},
    )

    assert message == {"role": "user", "content": "Task t1: current question"}
