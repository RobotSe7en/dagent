"""Runtime token and event adaptation helpers."""

from __future__ import annotations

from dagent.harness_runtime.tool_agent import LoopEventHandler, TokenHandler
from dagent.schemas import DAG


class _ResponseTokenSplitter:
    """Forward raw tokens and derive reasoning/content token events."""

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(
        self,
        *,
        on_raw: TokenHandler | None,
        on_event: LoopEventHandler | None,
    ) -> None:
        self._on_raw = on_raw
        self._on_event = on_event
        self._buf = ""
        self._inside = False

    def __call__(self, token: str) -> None:
        if self._on_raw is not None:
            self._on_raw(token)
        self._buf += token
        self._drain()

    def flush(self) -> None:
        if self._buf:
            self._emit_current_channel(self._buf)
        self._buf = ""
        self._inside = False

    def _emit_current_channel(self, delta: str) -> None:
        if not delta or self._on_event is None:
            return
        self._on_event({
            "type": "response_token",
            "channel": "reasoning" if self._inside else "content",
            "delta": delta,
        })

    def _drain(self) -> None:
        while self._buf:
            tag = self._CLOSE if self._inside else self._OPEN
            idx = self._buf.lower().find(tag)
            if idx != -1:
                self._emit_current_channel(self._buf[:idx])
                self._buf = self._buf[idx + len(tag):]
                self._inside = not self._inside
                continue

            keep = _partial_tag_suffix_len(self._buf, tag)
            safe = self._buf[:-keep] if keep else self._buf
            if safe:
                self._emit_current_channel(safe)
                self._buf = self._buf[len(safe):]
            return


def _partial_tag_suffix_len(text: str, tag: str) -> int:
    lower_text = text.lower()
    lower_tag = tag.lower()
    max_len = min(len(lower_text), len(lower_tag) - 1)
    for length in range(max_len, 0, -1):
        if lower_text.endswith(lower_tag[:length]):
            return length
    return 0


def _dag_event_emitter(on_event: LoopEventHandler | None):
    if on_event is None:
        return None
    last_payload: dict | None = None

    def emit(dag: DAG) -> None:
        nonlocal last_payload
        payload = dag.model_dump(mode="json")
        if payload == last_payload:
            return
        last_payload = payload
        on_event({"type": "dag", "dag": payload})

    return emit
