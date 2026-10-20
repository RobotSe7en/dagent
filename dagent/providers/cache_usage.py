"""Wire types that preserve cache counters before OpenAI's integer coercion.

Only the opt-in cache capture path uses these types. All other response fields
retain the client's existing models; cache counters must reach our validator
without turning a boolean or numeric string into a reported token count.
"""

from typing import Annotated, Any, Optional, Union, get_args

from openai import BaseModel
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.completion_usage import CompletionUsage
from openai.types.responses import (
    Response, ResponseCompletedEvent, ResponseFailedEvent,
    ResponseIncompleteEvent, ResponseStreamEvent,
)
from openai.types.responses.response_usage import ResponseUsage


class _CacheTokenDetails(BaseModel):
    cached_tokens: Any = None


class _CacheCompletionUsage(CompletionUsage):
    prompt_tokens_details: Optional[_CacheTokenDetails] = None


class CacheChatCompletion(ChatCompletion):
    usage: Optional[_CacheCompletionUsage] = None


class CacheChatCompletionChunk(ChatCompletionChunk):
    usage: Optional[_CacheCompletionUsage] = None


class _CacheResponseUsage(ResponseUsage):
    input_tokens_details: Optional[_CacheTokenDetails] = None


class CacheResponse(Response):
    usage: Optional[_CacheResponseUsage] = None


class _CacheResponseCompletedEvent(ResponseCompletedEvent):
    response: CacheResponse


class _CacheResponseFailedEvent(ResponseFailedEvent):
    response: CacheResponse


class _CacheResponseIncompleteEvent(ResponseIncompleteEvent):
    response: CacheResponse


_TERMINAL_EVENTS = {
    ResponseCompletedEvent: _CacheResponseCompletedEvent,
    ResponseFailedEvent: _CacheResponseFailedEvent,
    ResponseIncompleteEvent: _CacheResponseIncompleteEvent,
}
# Retain the client's discriminator and every other event model, including its
# partial-stream parsing. Only terminal responses need the cache-counter type.
CacheResponseStreamEvent = Annotated[
    Union[tuple(_TERMINAL_EVENTS.get(event, event)
                for event in get_args(get_args(ResponseStreamEvent)[0]))],
    *get_args(ResponseStreamEvent)[1:],
]
