"""Options for explicitly acknowledged ToolLoop streams."""

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class StreamPersistenceOptions(BaseModel):
    """Enable persistent delivery without constructing a diagnostic trace.

    Events enter an unbounded queue without blocking or merging deltas. Only
    durable batch publications wait for host acknowledgement; its optional
    timeout starts when the consumer receives the batch.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ack_timeout_seconds: FiniteFloat | None = Field(default=None, gt=0)
