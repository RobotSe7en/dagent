"""DAG edge schema."""

from __future__ import annotations

from pydantic import BaseModel

from dagent.schemas.value import ValueBinding


class DAGEdge(BaseModel):
    source: str
    target: str
    reason: str = ""
    when: ValueBinding | None = None
    branch: str | None = None
