"""LLM-backed feedback learner agent."""

from __future__ import annotations

from dataclasses import dataclass, field

from dagent.harness.profiled_agent import ProfiledAgent
from dagent.profiles import AgentProfile
from dagent.providers import ChatProvider
from dagent.schemas import TraceEvent


@dataclass(frozen=True)
class FeedbackLearning:
    notes: str
    preferences: list[str] = field(default_factory=list)
    eval_cases: list[dict] = field(default_factory=list)


class FeedbackLearnerAgent:
    def __init__(self, *, provider: ChatProvider, profile: AgentProfile) -> None:
        self.agent = ProfiledAgent(provider=provider, profile=profile)

    async def learn(
        self,
        *,
        feedback: str,
        trace_events: list[TraceEvent],
    ) -> FeedbackLearning:
        text = await self.agent.run_text(
            feedback=feedback,
            trace_json="[" + ",".join(event.model_dump_json() for event in trace_events) + "]",
        )
        return FeedbackLearning(notes=text)

