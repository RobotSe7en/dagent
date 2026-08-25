import asyncio

from examples import (
    agent_delegation,
    dag_design,
    dynamic_dag_builder_agent,
    runtime_registration_and_skills,
    tool_agent,
)


def test_tool_agent_example_uses_derived_tool_function_name(capsys) -> None:
    asyncio.run(tool_agent.main())

    lines = capsys.readouterr().out.strip().splitlines()

    assert lines[0] == "completed"
    assert lines[1] == "The echo tool returned hello."
    assert "tool_echo" in lines[2]


def test_agent_delegation_example_executes_registered_agent(capsys) -> None:
    asyncio.run(agent_delegation.main())

    lines = capsys.readouterr().out.strip().splitlines()

    assert lines[0] == "completed"
    assert lines[1] == "Final answer using the helper summary."
    assert "agent_helper" in lines[2]


def test_runtime_registration_example_uses_derived_tool_function_name(capsys) -> None:
    asyncio.run(runtime_registration_and_skills.main())

    lines = capsys.readouterr().out.strip().splitlines()

    assert lines == ["One sentence", "terse", "writing/drafting"]


def test_dynamic_dag_builder_example_runs_restricted_frontend(capsys) -> None:
    asyncio.run(dynamic_dag_builder_agent.main())

    lines = capsys.readouterr().out.strip().splitlines()

    assert lines == ["completed", "Report: found:Research dagent.", "sdk_builder"]


def test_dag_design_example_does_not_execute_tool(capsys) -> None:
    asyncio.run(dag_design.main())

    lines = capsys.readouterr().out.strip().splitlines()

    assert lines == [
        "Choosing the smallest valid graph.",
        "validation.started",
        "validation.passed",
        "proposal",
        "summary",
        "0",
    ]
