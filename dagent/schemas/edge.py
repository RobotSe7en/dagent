"""DAG edge schema."""

from __future__ import annotations

from pydantic import BaseModel


class DAGEdge(BaseModel):
    source: str
    target: str
    reason: str = ""

