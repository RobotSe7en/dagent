"""LLM-backed result quality reviewer."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging

from dagent.harness_runtime.profiled_agent import ProfiledAgent
from dagent.profiles import AgentProfile
from dagent.providers import ChatProvider


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewIssue:
    message: str
    node_id: str | None = None


@dataclass(frozen=True)
class ReviewResult:
    approved: bool
    issues: list[ReviewIssue] = field(default_factory=list)
    summary: str = ""


class ResultReviewerAgent:
    def __init__(self, *, provider: ChatProvider, profile: AgentProfile) -> None:
        self.agent = ProfiledAgent(provider=provider, profile=profile)

    async def review(
        self,
        *,
        user_request: str,
        final_answer: str,
        execution_context: str = "",
    ) -> ReviewResult:
        try:
            payload = await self.agent.run_json(
                task_content=(
                    "User request:\n{{ user_request }}\n\n"
                    "{% if execution_context %}"
                    "Execution context:\n{{ execution_context }}\n\n"
                    "{% endif %}"
                    "Final answer given to user:\n{{ final_answer }}\n\n"
                    "Review whether the final answer sufficiently addresses "
                    "the user's request.\n\n"
                    "Return ONLY one JSON object with this shape:\n"
                    "{{ response_schema }}\n\n"
                    "Do not include Markdown fences, explanations, or text outside the JSON object."
                ),
                user_request=user_request,
                final_answer=final_answer or "(no answer provided)",
                execution_context=execution_context,
                response_schema=json.dumps(
                    {
                        "approved": True,
                        "issues": [
                            {"message": "specific issue when approved is false", "node_id": None}
                        ],
                        "summary": "brief assessment",
                    },
                    ensure_ascii=False,
                ),
            )
        except ValueError as exc:
            logger.warning("Result reviewer returned invalid JSON; skipping review: %s", exc)
            return ReviewResult(
                approved=True,
                summary="Automated result review was skipped because the reviewer returned invalid JSON.",
            )
        issues = [
            ReviewIssue(
                message=str(issue.get("message", "")),
                node_id=issue.get("node_id"),
            )
            for issue in payload.get("issues", [])
            if isinstance(issue, dict) and issue.get("message")
        ]
        approved = bool(payload.get("approved", False))
        # Guard: rejected without issues; supplement a generic issue so the
        # agent has actionable feedback instead of an empty retry reason.
        if not approved and not issues:
            issues = [
                ReviewIssue(
                    message="The answer does not sufficiently address the user's request.",
                )
            ]
        return ReviewResult(
            approved=approved,
            issues=issues,
            summary=str(payload.get("summary", "")),
        )


def format_review_feedback(review: ReviewResult) -> str:
    lines = ["A reviewer assessed the result and found issues:"]
    if review.summary:
        lines.append(f"Summary: {review.summary}")
    for issue in review.issues:
        node_prefix = f"[{issue.node_id}] " if issue.node_id else ""
        lines.append(f"- {node_prefix}{issue.message}")
    lines.append("\nPlease address these issues.")
    return "\n".join(lines)
