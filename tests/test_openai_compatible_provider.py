from types import SimpleNamespace

import pytest

from dagent.config import ProviderConfig
from dagent.providers import OpenAICompatibleProvider


class FakeCompletions:
    def __init__(
        self,
        *,
        message=None,
        stream_chunks=None,
    ) -> None:
        self.kwargs = None
        self.message = message
        self.stream_chunks = stream_chunks

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if kwargs.get("stream"):
            if self.stream_chunks is not None:
                return FakeStream(self.stream_chunks)
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
        if self.message is not None:
            return SimpleNamespace(choices=[SimpleNamespace(message=self.message)])
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
    def __init__(self, *, message=None, stream_chunks=None) -> None:
        self.completions = FakeCompletions(
            message=message,
            stream_chunks=stream_chunks,
        )
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


@pytest.mark.asyncio
async def test_openai_compatible_provider_forwards_reasoning_and_extra_request_options() -> None:
    client = FakeClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="deepseek",
            api_key="local-key",
            reasoning={"enabled": True, "effort": "high", "budget_tokens": 512},
            extra_request_args={"temperature": 0},
            extra_body={"chat_template_kwargs": {"enable_thinking": True}},
        ),
        client=client,
    )

    await provider.chat([{"role": "user", "content": "hello"}])

    assert client.completions.kwargs["reasoning_effort"] == "high"
    assert client.completions.kwargs["thinking_token_budget"] == 512
    assert client.completions.kwargs["temperature"] == 0
    assert client.completions.kwargs["extra_body"] == {
        "thinking": {"type": "enabled"},
        "chat_template_kwargs": {"enable_thinking": True},
    }


@pytest.mark.asyncio
async def test_openai_compatible_provider_preserves_explicit_request_args_over_reasoning_defaults() -> None:
    client = FakeClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="deepseek",
            api_key="local-key",
            reasoning={"enabled": True, "effort": "high"},
            extra_request_args={"reasoning_effort": "low"},
            extra_body={"thinking": {"type": "disabled"}},
        ),
        client=client,
    )

    await provider.chat([{"role": "user", "content": "hello"}])

    assert client.completions.kwargs["reasoning_effort"] == "low"
    assert client.completions.kwargs["extra_body"] == {
        "thinking": {"type": "disabled"},
    }


@pytest.mark.asyncio
async def test_openai_compatible_provider_reads_reasoning_content_from_chat_response() -> None:
    client = FakeClient(
        message=SimpleNamespace(
            content="final answer",
            reasoning_content="deepseek reasoning",
            tool_calls=[],
        )
    )
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="deepseek",
            api_key="local-key",
        ),
        client=client,
    )

    response = await provider.chat([{"role": "user", "content": "hello"}])

    assert response.content == "final answer"
    assert response.reasoning_content == "deepseek reasoning"


@pytest.mark.asyncio
async def test_openai_compatible_provider_streams_reasoning_content_and_reasoning_aliases() -> None:
    client = FakeClient(
        stream_chunks=[
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(reasoning_content="deep", tool_calls=[])
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(reasoning=" thought", tool_calls=[])
                    )
                ]
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="final", tool_calls=[])
                    )
                ]
            ),
        ]
    )
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="http://localhost:8000/v1",
            model="deepseek",
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

    tokens = [
        (event.channel, event.content)
        for event in events
        if event.type == "token"
    ]
    assert tokens == [
        ("reasoning", "deep"),
        ("reasoning", " thought"),
        ("content", "final"),
    ]
    assert events[-1].response is not None
    assert events[-1].response.content == "final"
    assert events[-1].response.reasoning_content == "deep thought"
