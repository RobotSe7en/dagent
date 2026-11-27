"""Continue visual history. Run: uv run python -m examples.image_history"""

import asyncio
from contextlib import closing
from tempfile import TemporaryDirectory

import dagent
from dagent.providers import ChatResponse, MockProvider
from examples.image_uploads import sample_image


async def main() -> None:
    # Replies demonstrate wiring only; use a vision-capable Provider for analysis.
    provider = MockProvider([ChatResponse(content="The earlier images were analyzed.")] * 50)
    conversation = None
    target = dagent.ToolAgent(profile="conversation", capabilities=[])
    with TemporaryDirectory() as workspace, closing(dagent.Runner(provider=provider, workspace=workspace)) as runner:
        for index in range(20):
            result = await runner.run(target, input="Describe this image.", conversation=conversation,
                workspace_path=workspace, input_uploads=[dagent.ArtifactUpload(
                    "image.png", sample_image(f"#{index:02x}0000"), model_input="image")])
            assert result.status == "completed"
            conversation = result.conversation
        assert conversation.summary and conversation.summary.image_manifest
        print("Completed 20 image turns.")
        # The summary carries references; later requests carry text and an index notice.
        print("Archived image references:", len(conversation.summary.images))
        print("Images in the latest model request:", result.context_usage[-1].image_count)


if __name__ == "__main__":
    asyncio.run(main())
