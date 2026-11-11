"""Session-scoped run state helpers for the harness runtime."""

from __future__ import annotations

from dagent.schemas import RunState


class HarnessRuntimeSession:
    """Mutable in-process index for resumable run states."""

    def __init__(self) -> None:
        self.runs: dict[str, RunState] = {}
        self._review_index: dict[str, str] = {}

    def save_run_state(self, state: RunState) -> RunState:
        from dagent.harness_runtime.conversation_changes import current_changes
        persistent = current_changes() is not None
        # Persistent runs own detached inputs; only detached batches leave the
        # SDK. Share the active state internally instead of copying its complete
        # conversation at every save. Public queries still return detached data.
        stored = state if persistent else state.model_copy(deep=True)
        self.runs[stored.run_id] = stored
        self.discard_reviews_for_run(stored.run_id)
        if stored.pending_review is not None:
            self._review_index[stored.pending_review.review_id] = stored.run_id
        return stored if persistent else stored.model_copy(deep=True)

    def get_review_state(self, review_id: str) -> RunState | None:
        run_id = self._review_index.get(review_id)
        if run_id is not None:
            return self.runs.get(run_id)
        for state in self.runs.values():
            if state.pending_review is not None and state.pending_review.review_id == review_id:
                self._review_index[review_id] = state.run_id
                return state
        return None

    def discard_review(self, review_id: str) -> None:
        self._review_index.pop(review_id, None)

    def discard_reviews_for_run(self, run_id: str) -> None:
        stale_review_ids = [
            review_id
            for review_id, indexed_run_id in self._review_index.items()
            if indexed_run_id == run_id
        ]
        for review_id in stale_review_ids:
            self._review_index.pop(review_id, None)
