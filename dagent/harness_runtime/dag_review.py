"""LLM-backed DAG review agent."""

from __future__ import annotations

from dataclasses import dataclass, field

from dagent.harness_runtime.profiled_agent import ProfiledAgent
from dagent.profiles import AgentProfile
from dagent.providers import ChatProvider
from dagent.schemas import DAG


@dataclass(frozen=True)
class DAGReviewIssue:
    node_id: str | None
    severity: str
    message: str


@dataclass(frozen=True)
class DAGReviewResult:
    approved: bool
    issues: list[DAGReviewIssue] = field(default_factory=list)
    suggested_changes: list[dict] = field(default_factory=list)


class DAGReviewerAgent:
    def __init__(self, *, provider: ChatProvider, profile: AgentProfile) -> None:
        self.agent = ProfiledAgent(provider=provider, profile=profile)

    async def review(self, *, user_request: str, dag: DAG) -> DAGReviewResult:
        payload = await self.agent.run_json(
            task_content=(
                "User request:\n{{ user_request }}\n\n"
                "Proposed DAG JSON:\n{{ dag_json }}\n\n"
                "Review the DAG now."
            ),
            user_request=user_request,
            dag_json=dag.model_dump_json(indent=2),
        )
        return DAGReviewResult(
            approved=bool(payload.get("approved", False)),
            issues=[
                DAGReviewIssue(
                    node_id=issue.get("node_id"),
                    severity=str(issue.get("severity", "medium")),
                    message=str(issue.get("message", "")),
                )
                for issue in payload.get("issues", [])
                if isinstance(issue, dict)
            ],
            suggested_changes=[
                change
                for change in payload.get("suggested_changes", [])
                if isinstance(change, dict)
            ],
        )
