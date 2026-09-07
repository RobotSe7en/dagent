"""Read reasoning statistics from the serialized request, without retaining text."""

from collections import Counter
import json
from typing import Literal

from dagent.providers.model_io import ModelAssistantTurn, ModelRequest
from dagent.schemas.context import ModelCallMetadata, RequestReasoning


def observe_request_reasoning(
    body: bytes,
    request: ModelRequest,
    metadata: ModelCallMetadata,
    *,
    resolved_field: Literal["reasoning", "reasoning_content", "omit"],
    explicit_omit: bool,
) -> ModelCallMetadata:
    """Count Unicode text values after all client-side request overrides."""

    payload = json.loads(body)
    values: list[str] = []
    fields: set[Literal["reasoning", "reasoning_content", "omit"]] = set()
    if metadata.protocol == "chat_completions":
        for message in payload.get("messages", []):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for field in ("reasoning", "reasoning_content"):
                value = message.get(field)
                if isinstance(value, str) and value:
                    fields.add(field)
                    values.append(value)
    else:
        for item in payload.get("input", []):
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                continue
            parts = [*(item.get("content") or []), *(item.get("summary") or [])]
            text = "".join(part["text"] for part in parts
                           if isinstance(part, dict) and isinstance(part.get("text"), str))
            if text:
                fields.add("reasoning")
                values.append(text)

    retained = [item.reasoning for item in request.items
                if isinstance(item, ModelAssistantTurn) and item.reasoning]
    reasons = set(request.reasoning_omission_reasons)
    if Counter(retained) - Counter(values):
        if resolved_field == "omit":
            reasons.add("explicit_omit" if explicit_omit else "auto_unsupported")
        else:
            reasons.add("request_override")
    elif not retained and not reasons and not values:
        reasons.add("no_reasoning_available")
    observation = RequestReasoning(
        resolved_field=resolved_field,
        serialized_fields=tuple(sorted(fields)) if fields else ("omit",),
        serialized_items=len(values),
        serialized_characters=sum(map(len, values)),
        omission_reasons=tuple(sorted(reasons)),
    )
    return metadata.model_copy(update={"request_reasoning": observation})
