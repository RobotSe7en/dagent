"""Keep two ended runs while exporting independent results and checkpoints.

Run: uv run python -m examples.run_retention
"""

import asyncio
from contextlib import closing

import dagent
from dagent.providers import ChatResponse, MockProvider


async def main() -> None:
    provider = MockProvider([ChatResponse(content="done")] * 3)
    with closing(dagent.Runner(provider=provider, workspace="agent-workspace", skill_roots=[], max_retained_runs=2)) as runner:
        agent = dagent.ToolAgent(profile="conversation", capabilities=[])
        first = await runner.run(agent, input="first", run_id="first")
        exported = first.checkpoint.model_dump_json()
        second = await runner.run(agent, input="continue", conversation=first.conversation, run_id="second")
        await runner.run(agent, input="continue again", conversation=second.conversation, run_id="third")
        assert runner.run_state("first") is None
        assert runner.run_checkpoint("second") is not None
        assert first.status == "completed"
        assert dagent.RunCheckpoint.model_validate_json(exported).state.run_id == "first"
        print("retained: second, third")
        with closing(runner.derive()) as inherited, closing(runner.derive(max_retained_runs=None)) as unlimited:
            assert inherited.max_retained_runs == 2 and unlimited.max_retained_runs is None
            assert not inherited.runtime.runs
            print("derived runners start with no run history")


if __name__ == "__main__":
    asyncio.run(main())
