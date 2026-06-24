import asyncio

from examples import agent_delegation


def test_agent_delegation_example_executes_registered_agent(capsys) -> None:
    asyncio.run(agent_delegation.main())

    lines = capsys.readouterr().out.strip().splitlines()

    assert lines[0] == "completed"
    assert lines[1] == "Final answer using the helper summary."
    assert "agent_helper" in lines[2]
