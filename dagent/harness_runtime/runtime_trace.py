"""In-memory runtime trace recording for DAG runs."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from dagent.schemas import TraceEvent


class TraceRecorder:
    """Collects trace events for a single run."""

    def __init__(self, on_record: Callable[[TraceEvent], None] | None = None) -> None:
        self.events: list[TraceEvent] = []
        self.on_record = on_record

    def record(
        self,
        event_type: str,
        *,
        dag_id: str,
        node_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        event = TraceEvent(
            event_id=f"event_{uuid4().hex}",
            event_type=event_type,
            dag_id=dag_id,
            node_id=node_id,
            payload=payload or {},
        )
        self.events.append(event)
        if self.on_record is not None:
            self.on_record(event)
