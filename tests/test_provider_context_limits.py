from __future__ import annotations

import pytest

import dagent
from dagent.config import ProviderConfig
from dagent.harness_runtime.context import ContextAssembler
from dagent.providers import OpenAICompatibleProvider, ProviderCapabilityError
from dagent.schemas import ContextPolicy, ConversationState, UserMessage
from tests.test_model_protocols import DualProtocolClient, _simple_request


@pytest.mark.asyncio
async def test_unknown_window_allows_input_above_old_32k_default() -> None:
    prepared = await ContextAssembler().prepare(
        system_message={"role": "system", "content": "Be useful."},
        conversation=ConversationState(items=(UserMessage(content="文" * 40000),)),
        policy=ContextPolicy(),
    )
    assert prepared.usage.context_window_tokens == 131072
    assert 32768 < prepared.usage.estimated_input_tokens < 131072
    assert prepared.usage.configured_context_limit is None


@pytest.mark.parametrize("base_url", [
    "https://api.deepseek.com", "https://api.deepseek.com/v1/",
    "https://api.deepseek.com/beta", "https://api.deepseek.com:443/v1",
])
@pytest.mark.parametrize("model", [
    "deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp",
])
def test_official_deepseek_context_is_recognized_offline(base_url, model) -> None:
    client = DualProtocolClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(base_url=base_url, model=model),
        client=client,
    )
    assert provider.context_window_tokens == 1_048_576
    assert provider.model_context_window_tokens == 1_048_576
    assert provider.configured_context_window_tokens is None
    assert not client.raw_posts


@pytest.mark.parametrize("base_url,model", [
    ("https://proxy.example/v1", "deepseek-v4-pro"),
    ("https://api.deepseek.com.example/v1", "deepseek-v4-pro"),
    ("https://api.deepseek.com@proxy.example/v1", "deepseek-v4-pro"),
    ("https://api.deepseek.com:8443/v1", "deepseek-v4-pro"),
    ("https://api.deepseek.com/custom", "deepseek-v4-pro"),
    ("http://localhost:8000/v1", "deepseek-v4-pro"),
    ("https://api.deepseek.com", "unknown-future-model"),
])
def test_model_name_alone_does_not_apply_official_limit(base_url, model) -> None:
    provider = OpenAICompatibleProvider(
        ProviderConfig(base_url=base_url, model=model),
        client=DualProtocolClient(),
    )
    assert provider.context_window_tokens == 131072
    assert provider.model_context_window_tokens is None


@pytest.mark.asyncio
@pytest.mark.parametrize("token_counting", ["auto", "heuristic"])
@pytest.mark.parametrize("configured", [None, 65536])
async def test_runner_uses_official_limit_without_claiming_exact_count(
    tmp_path, token_counting, configured,
) -> None:
    client = DualProtocolClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="https://api.deepseek.com/v1",
            model="deepseek-v4-pro",
            protocol="chat_completions",
            token_counting=token_counting,
            context_window_tokens=configured,
        ),
        client=client,
    )
    runner = dagent.Runner(provider=provider, workspace=tmp_path)
    try:
        result = await runner.run(
            dagent.ToolAgent(profile="conversation", capabilities=[]), input="Hello",
        )
        usage = result.state.context_usage[-1]
        assert usage.context_window_tokens == (configured or 1_048_576)
        assert usage.model_context_window_tokens == 1_048_576
        assert usage.configured_context_limit == configured
        assert usage.server_max_model_len is None
        assert usage.estimator == "heuristic"
        assert result.checkpoint.plan.context_window_tokens == (configured or 1_048_576)
        assert not client.raw_posts
    finally:
        runner.close()


def test_explicit_context_cannot_exceed_official_limit() -> None:
    with pytest.raises(ProviderCapabilityError, match="1048577.*1048576"):
        OpenAICompatibleProvider(
            ProviderConfig(
                base_url="https://api.deepseek.com",
                model="deepseek-v4-pro",
                context_window_tokens=1_048_577,
            ),
            client=DualProtocolClient(),
        )


@pytest.mark.asyncio
async def test_deepseek_explicit_vllm_counting_fails_without_tokenize_call() -> None:
    client = DualProtocolClient()
    provider = OpenAICompatibleProvider(
        ProviderConfig(
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
            token_counting="vllm",
        ),
        client=client,
    )
    with pytest.raises(ProviderCapabilityError, match="does not provide vLLM"):
        await provider.count_tokens(_simple_request())
    assert not client.raw_posts
