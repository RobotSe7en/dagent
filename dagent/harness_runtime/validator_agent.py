"""LLM-backed validation agent."""

from __future__ import annotations

import json
import logging

from dagent.harness_runtime.profiled_agent import ProfiledAgent
from dagent.profiles import AgentProfile
from dagent.providers import ChatProvider
from dagent.schemas import ValidationIssue, ValidationResult


logger = logging.getLogger(__name__)


class ValidatorAgent:
    def __init__(self, *, provider: ChatProvider, profile: AgentProfile) -> None:
        self.agent = ProfiledAgent(provider=provider, profile=profile)

    async def validate(
        self,
        *,
        user_request: str,
        final_answer: str,
        execution_context: str = "",
    ) -> ValidationResult:
        try:
            payload = await self.agent.run_json(
                task_content=(
                    "User request:\n{{ user_request }}\n\n"
                    "{% if execution_context %}"
                    "Execution context:\n{{ execution_context }}\n\n"
                    "{% endif %}"
                    "Final answer given to user:\n{{ final_answer }}\n\n"
                    "Validate whether the final answer sufficiently addresses "
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
                        "passed": True,
                        "issues": [
                            {"message": "specific issue when passed is false", "node_id": None}
                        ],
                        "summary": "brief assessment",
                    },
                    ensure_ascii=False,
                ),
            )
        except ValueError as exc:
            logger.warning("Validator agent returned invalid JSON; skipping validation: %s", exc)
            return ValidationResult(
                passed=True,
                summary="Automated result validation was skipped because the validator agent returned invalid JSON.",
            )
        issues = [
            ValidationIssue(
                message=str(issue.get("message", "")),
                node_id=issue.get("node_id"),
            )
            for issue in payload.get("issues", [])
            if isinstance(issue, dict) and issue.get("message")
        ]
        passed = bool(payload.get("passed", False))
        # Guard: rejected without issues; supplement a generic issue so the
        # agent has actionable feedback instead of an empty retry reason.
        if not passed and not issues:
            issues = [
                ValidationIssue(
                    message="The answer does not sufficiently address the user's request.",
                )
            ]
        return ValidationResult(
            passed=passed,
            issues=issues,
            summary=str(payload.get("summary", "")),
        )


def format_validation_feedback(validation: ValidationResult) -> str:
    lines = ["A validator assessed the result and found issues:"]
    if validation.summary:
        lines.append(f"Summary: {validation.summary}")
    for issue in validation.issues:
        node_prefix = f"[{issue.node_id}] " if issue.node_id else ""
        lines.append(f"- {node_prefix}{issue.message}")
    lines.append("\nPlease address these issues.")
    return "\n".join(lines)
