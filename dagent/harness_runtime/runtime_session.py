"""Session-scoped run state helpers for the harness runtime."""

from __future__ import annotations

from dagent.schemas import RunState


class HarnessRuntimeSession:
    """Mutable in-process index for resumable run states."""

    def __init__(self) -> None:
        self.runs: dict[str, RunState] = {}
        self._review_index: dict[str, str] = {}

    def hydrate_run_state(self, state: RunState) -> RunState:
        return self.save_run_state(state)

    def save_run_state(self, state: RunState) -> RunState:
        stored = state.model_copy(deep=True)
        existing = self.runs.get(stored.run_id)
        if (
            existing is not None
            and existing.trace is not None
            and stored.trace is not None
            and existing.trace is not stored.trace
        ):
            stored = stored.model_copy(update={"trace": existing.trace.merge(stored.trace)})
        self.runs[stored.run_id] = stored
        self.discard_reviews_for_run(stored.run_id)
        if stored.pending_review is not None:
            self._review_index[stored.pending_review.review_id] = stored.run_id
        return stored.model_copy(deep=True)

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
