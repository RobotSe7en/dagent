"""High-level dagent SDK facade."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from dagent.config import DagentConfig
from dagent.factory import create_harness_runtime
from dagent.harness_runtime import HarnessRuntime
from dagent.harness_runtime.artifacts import ArtifactUpload
from dagent.harness_runtime.review_policy import ReviewLevel
from dagent.harness_runtime.runtime import RuntimeMode
from dagent.harness_runtime.tool_agent import LoopEventHandler, TokenHandler
from dagent.result import RunResult
from dagent.review import ReviewDecision
from dagent.schemas import CapabilityDefinition, DAGRun, DAGSpec


class DAgent:
    """User-facing agent object backed by HarnessRuntime."""

    def __init__(
        self,
        *,
        runtime: HarnessRuntime | None = None,
        config: DagentConfig | None = None,
        workspace_root: str | Path = ".",
    ) -> None:
        self._runtime = runtime or create_harness_runtime(
            config=config,
            workspace_root=workspace_root,
        )

    @classmethod
    def from_config(
        cls,
        *,
        config: DagentConfig | None = None,
        workspace_root: str | Path = ".",
    ) -> "DAgent":
        return cls(config=config, workspace_root=workspace_root)

    @property
    def runtime(self) -> HarnessRuntime:
        return self._runtime

    def register_capability(
        self,
        capability: Any,
        handler: Any = None,
        *,
        supports_context: bool | None = None,
    ) -> CapabilityDefinition:
        if handler is None and supports_context is None:
            return self._runtime.register_capability(capability)
        return self._runtime.register_capability(
            capability,
            handler,
            supports_context=supports_context,
        )

    def replace_capability(
        self,
        capability: Any,
        handler: Any = None,
        *,
        supports_context: bool | None = None,
    ) -> CapabilityDefinition:
        if handler is None and supports_context is None:
            return self._runtime.replace_capability(capability)
        return self._runtime.replace_capability(
            capability,
            handler,
            supports_context=supports_context,
        )

    def with_capabilities(self, capabilities: Iterable[Any]) -> "DAgent":
        for item in capabilities:
            self.register_capability(item)
        return self

    async def run(
        self,
        message: str,
        *,
        mode: RuntimeMode = "auto",
        review: ReviewLevel | None = None,
        review_level: ReviewLevel | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult:
        response = await self._runtime.handle_message(
            message,
            mode=mode,
            review_level=review_level or review or "fast",
            on_token=on_token,
            on_event=on_event,
        )
        return RunResult(response)

    async def resume(
        self,
        decision: ReviewDecision,
        *,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> RunResult | None:
        response = await self._runtime.resume_review(
            decision.review_id,
            dag=decision.dag,
            approved=decision.approved,
            review_level=decision.review_level,
            on_token=on_token,
            on_event=on_event,
        )
        return RunResult(response) if response is not None else None

    async def run_spec(
        self,
        spec: DAGSpec,
        *,
        workspace_root: str | Path = ".dagent-runs",
        artifact_uploads: dict[str, list[ArtifactUpload]] | None = None,
        on_token: TokenHandler | None = None,
        on_event: LoopEventHandler | None = None,
    ) -> DAGRun:
        return await self._runtime.run_dag_spec(
            spec,
            workspace_root=workspace_root,
            artifact_uploads=artifact_uploads,
            on_token=on_token,
            on_event=on_event,
        )
