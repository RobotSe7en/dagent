"""Runtime token and event adaptation helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from dagent.schemas import DAG


TokenHandler = Callable[[str], None]
LoopEventHandler = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class ResponseStreamContext:
    run_id: str | None = None
    response_id: str = ""
    model_step: int | None = None
    dag_id: str | None = None
    node_id: str | None = None
    parent_capability_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        run_id: str | None = None,
        model_step: int | None = None,
        dag_id: str | None = None,
        node_id: str | None = None,
        parent_capability_id: str | None = None,
    ) -> "ResponseStreamContext":
        return cls(
            run_id=run_id,
            response_id=f"resp_{uuid4().hex}",
            model_step=model_step,
            dag_id=dag_id,
            node_id=node_id,
            parent_capability_id=parent_capability_id,
        )

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"response_id": self.response_id}
        for key in ("run_id", "model_step", "dag_id", "node_id", "parent_capability_id"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


class _ResponseTokenSplitter:
    """Stream exactly one model call: raw tokens, channel deltas, and boundaries.

    ``start()`` emits ``response_started``; the first token starts lazily when
    callers do not want empty model-call brackets. ``finish()`` flushes any
    buffered text and emits ``response_finished`` only for a started stream.
    Every emitted event carries the splitter's identity fields
    (``response_id``, and ``model_step`` / ``run_id`` when known), so events
    from concurrent model calls stay attributable. Whitespace between
    ``</think>`` and the answer is dropped from the content channel so streamed
    content matches ``output_text``.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(
        self,
        *,
        on_raw: TokenHandler | None,
        on_event: LoopEventHandler | None,
        context: ResponseStreamContext,
    ) -> None:
        self._on_raw = on_raw
        self._on_event = on_event
        self._buf = ""
        self._inside = False
        self._strip_content_whitespace = False
        self._started = False
        self._finished = False
        self.response_id = context.response_id
        self._identity = context.payload()

    def __call__(self, token: str) -> None:
        self.start()
        if self._on_raw is not None:
            self._on_raw(token)
        self._buf += token
        self._drain()

    def start(self) -> None:
        if self._started or self._finished:
            return
        self._started = True
        self._emit({"type": "response_started"})

    def finish(self) -> None:
        if self._finished:
            return
        if not self._started:
            self._finished = True
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


def response_token_stream(
    *,
    on_raw: TokenHandler | None,
    on_event: LoopEventHandler | None,
    context: ResponseStreamContext,
) -> _ResponseTokenSplitter | None:
    if on_raw is None and on_event is None:
        return None
    return _ResponseTokenSplitter(on_raw=on_raw, on_event=on_event, context=context)


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
