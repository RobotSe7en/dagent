"""Runtime token and event adaptation helpers."""

from __future__ import annotations

from dagent.harness_runtime.tool_agent import LoopEventHandler, TokenHandler
from dagent.schemas import DAG, TraceEvent


class _ThinkTagFilter:
    """Streaming filter that selectively forwards tokens based on <think> blocks.

    Two modes (controlled by ``keep``):

    * ``keep="inside"``: forward only tokens inside ``<think>...</think>``
      (including the tags themselves). Used during ToolAgentLoop execution so the
      user sees the model's reasoning but not the answer (which will come from
      ``_summarize()``).

    * ``keep="outside"``: forward only tokens outside ``<think>...</think>``.
      Used during ``_summarize()`` so the user sees the final answer but not
      the summarizer's internal reasoning.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self, downstream: TokenHandler, *, keep: str = "inside") -> None:
        assert keep in {"inside", "outside"}
        self._downstream = downstream
        self._keep = keep
        self._buf = ""
        self._inside = False

    def _should_emit(self) -> bool:
        return (self._keep == "inside") == self._inside

    def __call__(self, token: str) -> None:
        self._buf += token
        while self._buf:
            if not self._inside:
                idx = self._buf.lower().find(self._OPEN)
                if idx == -1:
                    # No opening tag. Keep a small tail in case a tag is
                    # split across tokens, but only if '<' is present in
                    # the tail; otherwise the tail cannot start a tag.
                    keep = len(self._OPEN) - 1
                    if len(self._buf) > keep and "<" not in self._buf[-keep:]:
                        # No chance of a partial tag; flush everything.
                        if self._should_emit():
                            self._downstream(self._buf)
                        self._buf = ""
                    else:
                        safe = self._buf[:-keep] if len(self._buf) > keep else ""
                        if safe and self._should_emit():
                            self._downstream(safe)
                        self._buf = self._buf[len(safe):]
                    return
                # Text before <think>
                before = self._buf[:idx]
                if before and self._should_emit():
                    self._downstream(before)
                # Emit the tag itself if keeping inside.
                tag_end = idx + len(self._OPEN)
                self._inside = True
                if self._should_emit():
                    self._downstream(self._buf[idx:tag_end])
                self._buf = self._buf[tag_end:]
            else:
                idx = self._buf.lower().find(self._CLOSE)
                if idx == -1:
                    safe_len = len(self._buf) - (len(self._CLOSE) - 1)
                    if safe_len > 0 and "<" not in self._buf[-max(len(self._CLOSE) - 1, 0):]:
                        # No partial close tag possible; flush all.
                        if self._should_emit():
                            self._downstream(self._buf)
                        self._buf = ""
                    elif safe_len > 0:
                        safe = self._buf[:safe_len]
                        if self._should_emit():
                            self._downstream(safe)
                        self._buf = self._buf[safe_len:]
                    return
                # Text inside think + closing tag
                tag_end = idx + len(self._CLOSE)
                inside_text = self._buf[:idx]
                if inside_text and self._should_emit():
                    self._downstream(inside_text)
                # Closing tag; about to leave inside.
                if self._should_emit():
                    self._downstream(self._buf[idx:tag_end])
                self._buf = self._buf[tag_end:]
                self._inside = False


def _trace_event_emitter(on_event: LoopEventHandler | None):
    if on_event is None:
        return None

    def emit(trace: TraceEvent) -> None:
        on_event({"type": "trace", "event": trace.model_dump(mode="json")})

    return emit


def _dag_event_emitter(on_event: LoopEventHandler | None):
    if on_event is None:
        return None

    def emit(dag: DAG) -> None:
        on_event({"type": "dag", "dag": dag.model_dump(mode="json")})

    return emit
