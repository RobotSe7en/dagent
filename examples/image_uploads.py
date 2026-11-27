"""Upload two visual inputs. Run: uv run python -m examples.image_uploads"""

import asyncio
from contextlib import closing
from io import BytesIO
from tempfile import TemporaryDirectory

from PIL import Image

import dagent
from dagent.providers import ChatResponse, MockProvider


def sample_image(color: str = "red") -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 24), color).save(output, format="PNG")
    return output.getvalue()


async def main() -> None:
    # Replace this deterministic provider with a vision-capable Provider for analysis.
    provider = MockProvider([ChatResponse(content="Two image inputs were submitted.")])
    with TemporaryDirectory() as workspace, closing(dagent.Runner(provider=provider, workspace=workspace)) as runner:
        result = await runner.run(
            dagent.ToolAgent(profile="conversation", capabilities=[]),
            input="Compare these images.",
            input_uploads=[
                dagent.ArtifactUpload("before.png", sample_image(), model_input="image"),
                dagent.ArtifactUpload("after.png", sample_image("blue"), model_input="image"),
            ],
        )
        assert result.conversation.schema_version == 5
        assert result.context_usage[-1].image_count == 2
        print(result.output_text)


if __name__ == "__main__":
    asyncio.run(main())
