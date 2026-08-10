from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from dagent_tui.app import ChatMessage, DagentTui, ReviewScreen
from dagent_tui.models import (
    Conversation,
    ConversationMessage,
    Navigation,
    ReviewLevel,
    RunTarget,
    StreamEnvelope,
)


class FakeApi:
    base_url = "http://fake"

    def __init__(self, *, review: bool = False) -> None:
        self.conversation = Conversation(id="conv-1", title="Existing")
        self.review = review
        self.closed = False
        self.create_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.resume_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[str] = []

    async def aclose(self) -> None:
        self.closed = True

    async def health(self) -> None:
        return None

    async def navigation(self) -> Navigation:
        return Navigation(standalone=(self.conversation,))

    async def create_conversation(
        self,
        title: str,
        *,
        project_id: str | None = None,
        kind: str = "chat",
    ) -> Conversation:
        self.create_calls.append({"title": title, "project_id": project_id, "kind": kind})
        self.conversation = Conversation(
            id="conv-new",
            title=title,
            project_id=project_id,
            kind=kind,
        )
        return self.conversation

    async def messages(self, conversation: Conversation) -> tuple[ConversationMessage, ...]:
        return (
            ConversationMessage(
                role="assistant",
                content="Previous answer",
                timeline=(
                    {"type": "reasoning", "content": "Previous reasoning", "closed": True},
                    {
                        "type": "capability",
                        "status": "completed",
                        "event": {
                            "type": "capability.call.started",
                            "capability_id": "tool.cached",
                        },
                        "result": {
                            "type": "capability.call.completed",
                            "capability_id": "tool.cached",
                        },
                    },
                ),
            ),
        )

    async def stream_message(
        self,
        conversation: Conversation,
        message: str,
        *,
        target: RunTarget,
        review_level: ReviewLevel,
    ) -> AsyncIterator[StreamEnvelope]:
        self.stream_calls.append(
            {"conversation": conversation, "message": message, "target": target, "review_level": review_level}
        )
        yield StreamEnvelope(type="run.started", data={"kind": "tool"}, run_id="run-1")
        yield StreamEnvelope(
            type="capability.call.started",
            data={"invocation_id": "inv-1", "capability_id": "tool.echo", "arguments": {}},
            run_id="run-1",
        )
        if self.review:
            dag = {
                "dag_id": "dag-1",
                "status": "review_required",
                "nodes": [],
                "edges": [],
            }
            pending = {
                "review_id": "review-1",
                "kind": "initial_dag",
                "message": "Approve this DAG?",
                "proposed_dag": dag,
            }
            yield StreamEnvelope(type="dag.updated", data={"dag": dag}, run_id="run-1")
            yield StreamEnvelope(type="review.required", data=pending, run_id="run-1")
            yield StreamEnvelope(
                type="run.finished",
                data={
                    "result": {
                        "output_text": "",
                        "state": {
                            "status": "awaiting_review",
                            "pending_review": pending,
                            "dag": dag,
                        },
                    }
                },
                run_id="run-1",
            )
            return
        yield StreamEnvelope(
            type="response.content.delta",
            data={"response_id": "response-1", "delta": "New answer"},
            run_id="run-1",
        )
        yield StreamEnvelope(
            type="run.finished",
            data={"result": {"output_text": "New answer", "state": {"status": "completed"}}},
            run_id="run-1",
        )

    async def resume_review(
        self,
        conversation: Conversation,
        review: dict[str, Any],
        *,
        approved: bool,
        feedback: str,
        review_level: ReviewLevel,
        dag: dict[str, Any] | None,
    ) -> AsyncIterator[StreamEnvelope]:
        self.resume_calls.append(
            {"approved": approved, "feedback": feedback, "review_level": review_level, "dag": dag}
        )
        yield StreamEnvelope(type="run.started", data={"kind": "dynamic_dag"}, run_id="run-1")
        yield StreamEnvelope(
            type="run.finished",
            data={"result": {"output_text": "Reviewed answer", "state": {"status": "completed"}}},
            run_id="run-1",
        )

    async def cancel_run(self, run_id: str) -> bool:
        self.cancel_calls.append(run_id)
        return True


class BlockingFakeApi(FakeApi):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def stream_message(
        self,
        conversation: Conversation,
        message: str,
        *,
        target: RunTarget,
        review_level: ReviewLevel,
    ) -> AsyncIterator[StreamEnvelope]:
        yield StreamEnvelope(type="run.started", data={"kind": "tool"}, run_id="run-blocked")
        await self.release.wait()


async def wait_until(pilot: Any, predicate: Any, attempts: int = 30) -> None:
    for _ in range(attempts):
        await pilot.pause()
        if predicate():
            return
    raise AssertionError("Condition was not reached")


@pytest.mark.asyncio
async def test_app_loads_history_and_renders_streamed_result() -> None:
    api = FakeApi()
    app = DagentTui(api=api)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 44)) as pilot:
        await wait_until(pilot, lambda: app._conversation is not None)
        prompt = app.query_one("#prompt")
        prompt.value = "Run it"
        prompt.focus()
        await pilot.press("enter")
        await wait_until(pilot, lambda: not app._busy and app._last_prompt == "Run it")

        contents = [message.content for message in app.query(ChatMessage)]
        assert contents == ["Previous reasoning", "Previous answer", "Run it", "New answer"]
        activity = "\n".join(line.text for line in app.query_one("#activity").lines)
        assert "tool.cached" in activity
        assert "tool.echo" in activity

        await pilot.press("ctrl+r")
        await wait_until(pilot, lambda: len(api.stream_calls) == 2 and not app._busy)
        assert [call["message"] for call in api.stream_calls] == ["Run it", "Run it"]

    assert api.closed


@pytest.mark.asyncio
async def test_app_approves_review_and_resumes_same_run() -> None:
    api = FakeApi(review=True)
    app = DagentTui(api=api)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 44)) as pilot:
        await wait_until(pilot, lambda: app._conversation is not None)
        prompt = app.query_one("#prompt")
        prompt.value = "Plan it"
        prompt.focus()
        await pilot.press("enter")
        await wait_until(pilot, lambda: isinstance(app.screen, ReviewScreen))
        app.screen.query_one("#review-feedback").value = "Looks good"
        await pilot.click("#approve-review")
        await wait_until(pilot, lambda: not app._busy and bool(api.resume_calls))

        assert api.resume_calls == [
            {
                "approved": True,
                "feedback": "Looks good",
                "review_level": "fast",
                "dag": {
                    "dag_id": "dag-1",
                    "status": "review_required",
                    "nodes": [],
                    "edges": [],
                },
            }
        ]
        assert any(message.content == "Reviewed answer" for message in app.query(ChatMessage))


@pytest.mark.asyncio
async def test_ctrl_c_cancels_active_host_run() -> None:
    api = BlockingFakeApi()
    app = DagentTui(api=api)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 44)) as pilot:
        await wait_until(pilot, lambda: app._conversation is not None)
        prompt = app.query_one("#prompt")
        prompt.value = "Wait"
        prompt.focus()
        await pilot.press("enter")
        await wait_until(pilot, lambda: app._active_run_id == "run-blocked")
        await pilot.press("ctrl+c")
        await wait_until(pilot, lambda: api.cancel_calls == ["run-blocked"] and not app._busy)

        assert any("cancellation requested" in message.content for message in app.query(ChatMessage))


@pytest.mark.asyncio
async def test_new_dag_target_creates_dynamic_dag_conversation() -> None:
    api = FakeApi()
    app = DagentTui(api=api)  # type: ignore[arg-type]

    async with app.run_test(size=(140, 44)) as pilot:
        await wait_until(pilot, lambda: app._conversation is not None)
        await pilot.press("ctrl+n")
        await wait_until(pilot, lambda: app._conversation is None)
        app.query_one("#target-select").value = "dag"
        prompt = app.query_one("#prompt")
        prompt.value = "Build a plan"
        prompt.focus()
        await pilot.press("enter")
        await wait_until(pilot, lambda: bool(api.create_calls) and not app._busy)

        assert api.create_calls == [
            {"title": "Build a plan", "project_id": None, "kind": "dynamic_dag"}
        ]
        assert api.stream_calls[0]["target"] == "dag"
