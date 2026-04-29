from dagent.profiles import AgentProfile
from dagent.state import PromptBuilder, PromptRequest
from dagent.tools.registry import Tool


def test_prompt_builder_assembles_profile_and_dynamic_sections() -> None:
    profile = AgentProfile(
        name="planner",
        role="planner",
        layers=["soul.md", "agent.md"],
        layer_contents={
            "soul.md": "Planner soul",
            "agent.md": "Planner agent instructions",
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
    assert "Planner soul" in messages[0]["content"]
    assert "Planner agent instructions" in messages[0]["content"]
    assert "read_file: Read a file." in messages[0]["content"]
    assert "code_review" in messages[0]["content"]
    assert "Remember narrow boundaries." in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "Task t1: hello"}
