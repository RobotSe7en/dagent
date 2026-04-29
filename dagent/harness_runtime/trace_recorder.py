"""In-memory trace recording for DAG runs."""

from __future__ import annotations

from uuid import uuid4

from dagent.schemas import TraceEvent


class TraceRecorder:
    """Collects trace events for a single run."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def record(
        self,
        event_type: str,
        *,
        dag_id: str,
        node_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        self.events.append(
            TraceEvent(
                event_id=f"event_{uuid4().hex}",
                event_type=event_type,
                dag_id=dag_id,
                node_id=node_id,
                payload=payload or {},
            )
        )

