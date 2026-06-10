"""Runtime token and event adaptation helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from dagent.schemas import DAG


TokenHandler = Callable[[str], None]
LoopEventHandler = Callable[[dict[str, Any]], None]


class _ResponseTokenSplitter:
    """Stream exactly one model call: raw tokens, channel deltas, and boundaries.

    Creating the splitter emits ``response_started``; ``finish()`` flushes any
    buffered text and emits ``response_finished``. Every emitted event carries
    the splitter's identity fields (``response_id``, and ``model_step`` /
    ``run_id`` when known), so events from concurrent model calls stay
    attributable. Whitespace between ``</think>`` and the answer is dropped
    from the content channel so streamed content matches ``output_text``.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(
        self,
        *,
        on_raw: TokenHandler | None,
        on_event: LoopEventHandler | None,
        run_id: str | None = None,
        model_step: int | None = None,
    ) -> None:
        self._on_raw = on_raw
        self._on_event = on_event
        self._buf = ""
        self._inside = False
        self._strip_content_whitespace = False
        self._finished = False
        self.response_id = f"resp_{uuid4().hex}"
        self._identity: dict[str, Any] = {"response_id": self.response_id}
        if model_step is not None:
            self._identity["model_step"] = model_step
        if run_id is not None:
            self._identity["run_id"] = run_id
        self._emit({"type": "response_started"})

    def __call__(self, token: str) -> None:
        if self._on_raw is not None:
            self._on_raw(token)
        self._buf += token
        self._drain()

    def finish(self) -> None:
        if self._finished:
            return
        if self._buf:
            self._emit_current_channel(self._buf)
        self._buf = ""
        self._inside = False
        self._finished = True
        self._emit({"type": "response_finished"})

    def _emit(self, event: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event({**event, **self._identity})

    def _emit_current_channel(self, delta: str) -> None:
        if not self._inside and self._strip_content_whitespace:
            delta = delta.lstrip()
            if delta:
                self._strip_content_whitespace = False
        if not delta:
            return
        self._emit({
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
                if not self._inside:
                    self._strip_content_whitespace = True
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
