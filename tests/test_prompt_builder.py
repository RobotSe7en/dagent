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


def test_prompt_builder_assembles_initial_agent_messages_with_runtime_context() -> None:
    builder = PromptBuilder()
    messages = builder.build_initial_messages(
        system_message={"role": "system", "content": "System prompt."},
        conversation_history=[
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": "previous question"},
            {"role": "assistant", "content": "previous answer"},
            {"role": "tool", "content": "ignored"},
        ],
        current_user_message={"role": "user", "content": "current question"},
        runtime_context="Use prior DAG result.",
    )

    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert messages[0]["content"] == "System prompt."
    assert messages[1]["content"] == "previous question"
    assert messages[2]["content"] == "previous answer"
    assert messages[3]["content"] == "current question\n\n## Runtime Context\nUse prior DAG result."
