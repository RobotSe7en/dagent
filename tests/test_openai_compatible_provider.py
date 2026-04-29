from types import SimpleNamespace

import pytest

from dagent.config import ProviderConfig
from dagent.providers import OpenAICompatibleProvider


class FakeCompletions:
    def __init__(self) -> None:
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="<think>hidden</think>\n\ndone",
                        tool_calls=[
                            SimpleNamespace(
                                id="call_1",
                                function=SimpleNamespace(
                                    name="read_file",
                                    arguments='{"path": "notes.txt"}',
                                ),
                            )
                        ],
                    )
                )
            ]
        )


class FakeClient:
    def __init__(self) -> None:
        self.completions = FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


@pytest.mark.asyncio
async def test_openai_compatible_provider_uses_config_and_converts_tool_calls() -> None:
    client = FakeClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            api_key="local-key",
            timeout_seconds=12,
        ),
        client=client,
    )

    response = await provider.chat(
        [{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {"name": "read_file"}}],
    )

    assert client.completions.kwargs["model"] == "qwen3"
    assert client.completions.kwargs["messages"] == [
        {"role": "user", "content": "hello"}
    ]
    assert client.completions.kwargs["tools"] == [
        {"type": "function", "function": {"name": "read_file"}}
    ]
    assert response.content == "done"
    assert response.tool_calls[0].id == "call_1"
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "notes.txt"}
