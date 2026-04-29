"""Generic LLM-backed agent role loaded from an editable profile."""

from __future__ import annotations

import json
import re
from typing import Any

from dagent.profiles import AgentProfile
from dagent.providers import ChatProvider


class ProfiledAgent:
    """Runs a single profile-defined LLM role."""

    def __init__(
        self,
        *,
        provider: ChatProvider,
        profile: AgentProfile,
    ) -> None:
        self.provider = provider
        self.profile = profile

    async def run_text(self, **prompt_values: Any) -> str:
        response = await self.provider.chat(
            [
                {"role": "system", "content": self.profile.system_prompt},
                {
                    "role": "user",
                    "content": self.profile.render_user_prompt(**prompt_values),
                },
            ]
        )
        return response.content

    async def run_json(self, **prompt_values: Any) -> dict[str, Any]:
        return extract_json_object(await self.run_text(**prompt_values))


def extract_json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("Agent response did not contain a JSON object.")
        parsed = json.loads(stripped[start : end + 1])

    if not isinstance(parsed, dict):
        raise ValueError("Agent response JSON must be an object.")
    return parsed

