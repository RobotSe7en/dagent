"""Runner entrypoints for the dagent SDK."""

from __future__ import annotations

import asyncio
from pathlib import Path

from dagent.agent import DAgent
from dagent.harness_runtime.artifacts import ArtifactUpload
from dagent.harness_runtime.review_policy import ReviewLevel
from dagent.harness_runtime.runtime import RuntimeMode
from dagent.harness_runtime.tool_agent import LoopEventHandler, TokenHandler
from dagent.result import RunResult
from dagent.review import ReviewDecision
from dagent.schemas import DAGRun, DAGSpec


class Runner:
    """OpenAI-style static runner facade."""

    @classmethod
    async def run(
        cls,
        agent: DAgent,
        input: str,
        *,
        mode: RuntimeMode = "auto",
        review: ReviewLevel | None = None,
        review_level: ReviewLevel | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        return await agent.run(
            input,
            mode=mode,
            review=review,
            review_level=review_level,
            on_token=on_token,
            on_event=on_event,
        )

    @classmethod
    async def resume(
        cls,
        agent: DAgent,
        decision: ReviewDecision,
        *,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult | None:
        return await agent.resume(
            decision,
            on_token=on_token,
            on_event=on_event,
        )

    @classmethod
    async def run_spec(
        cls,
        agent: DAgent,
        spec: DAGSpec,
        *,
        workspace_root: str | Path = ".dagent-runs",
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> DAGRun:
        return await agent.run_spec(
            spec,
            workspace_root=workspace_root,
            artifact_uploads=artifact_uploads,
            on_token=on_token,
            on_event=on_event,
        )

    @classmethod
    def run_sync(
        cls,
        agent: DAgent,
        input: str,
        **kwargs,
    ) -> RunResult:
        _ensure_no_running_loop("Runner.run_sync")
        return asyncio.run(cls.run(agent, input, **kwargs))

    @classmethod
    def resume_sync(
        cls,
        agent: DAgent,
        decision: ReviewDecision,
        **kwargs,
    ) -> RunResult | None:
        _ensure_no_running_loop("Runner.resume_sync")
        return asyncio.run(cls.resume(agent, decision, **kwargs))

    @classmethod
    def run_spec_sync(
        cls,
        agent: DAgent,
        spec: DAGSpec,
        **kwargs,
    ) -> DAGRun:
        _ensure_no_running_loop("Runner.run_spec_sync")
        return asyncio.run(cls.run_spec(agent, spec, **kwargs))


def _ensure_no_running_loop(method_name: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(f"{method_name} cannot be used inside a running event loop; use the async method instead.")
