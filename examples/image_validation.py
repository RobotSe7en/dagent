"""Validate visual DAG evidence. Run: uv run python -m examples.image_validation"""

import asyncio
import json
from contextlib import closing
from tempfile import TemporaryDirectory

import dagent
from dagent.providers import ChatResponse, MockProvider
from examples.image_uploads import sample_image


@dagent.tool
def screenshot() -> dagent.ImageData:
    return dagent.ImageData.from_bytes(sample_image("red"))


def final_answer(answer: str) -> ChatResponse:
    return ChatResponse(content=json.dumps({
        "action": "final_answer", "builder_code": None, "answer": answer, "rerun_nodes": [],
    }))


async def main() -> None:
    # Replace the deterministic replies with a vision-capable provider for analysis.
    provider = MockProvider([
        ChatResponse(content=json.dumps({
            "action": "propose_plan",
            "builder_code": (
                'dag = dagent.Dag("inspect")\n'
                'dag.add_node(dagent.Node("capture", target="tool.screenshot"))\n'
            ),
            "answer": None, "rerun_nodes": [],
        })),
        final_answer("The square is blue."),
        ChatResponse(content=json.dumps({
            "passed": False,
            "issues": [{"message": "The screenshot is red. Correct the color."}],
            "summary": "The answer disagrees with the image.",
        })),
        final_answer("The square is red."),
    ])
    with TemporaryDirectory() as workspace, closing(dagent.Runner(
        provider=provider, workspace=workspace, validator="validator_agent",
        planner_frontend="sdk_builder",
    )) as runner:
        result = await runner.run(dagent.DagAgent(capabilities=[screenshot]),
                                  input="Describe the screenshot.")
        assert provider.requests[2]["messages"][-1]["content"][-1]["type"] == "image_url"
        assert result.output_text == "The square is red."
        print(result.output_text)


if __name__ == "__main__":
    asyncio.run(main())
