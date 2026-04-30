from types import SimpleNamespace

import pytest

from dagent.config import ProviderConfig
from dagent.providers import OpenAICompatibleProvider


class FakeCompletions:
    def __init__(self) -> None:
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if kwargs.get("stream"):
            return FakeStream(
                [
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(content="<think>visible</think>\n", tool_calls=[])
                            )
                        ]
                    ),
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(content="done", tool_calls=[])
                            )
                        ]
                    ),
                ]
            )
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


class FakeStream:
    def __init__(self, chunks) -> None:
        self._chunks = chunks

    def __aiter__(self):
        self._iterator = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


@pytest.mark.asyncio
async def test_openai_compatible_provider_uses_config_and_converts_tool_calls() -> None:
    client = FakeClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            api_key="local-key",
            timeout_seconds=12,
            strip_thinking=True,
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


@pytest.mark.asyncio
async def test_openai_compatible_provider_streams_tokens_without_stripping_think() -> None:
    client = FakeClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="qwen3",
            api_key="local-key",
        ),
        client=client,
    )

    events = [
        event
        async for event in provider.stream_chat(
            [{"role": "user", "content": "hello"}],
        )
    ]

    assert client.completions.kwargs["stream"] is True
    assert [event.content for event in events if event.type == "token"] == [
        "<think>visible</think>\n",
        "done",
    ]
    assert events[-1].response is not None
    assert events[-1].response.content == "<think>visible</think>\ndone"
