"""Harness-level control tools exposed to the top agent loop."""

from __future__ import annotations

from typing import Any


DAG_CREATOR_NAME = "dag_creator"


def dag_creator_tool_definition() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": DAG_CREATOR_NAME,
            "description": (
                "Create a reviewable DAG only for complex orchestration that benefits "
                "from node-level planning, human review, parallelism, resumability, "
                "or multi-agent coordination. Do not use for greetings, simple Q&A, "
                "single tool calls, or ordinary short serial work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "request": {
                        "type": "string",
                        "description": "The focused task request to compile into a DAG.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this request needs DAG orchestration.",
                    },
                },
                "required": ["request", "reason"],
                "additionalProperties": False,
            },
        },
    }
