"""Options for explicitly acknowledged ToolLoop streams."""

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictInt


class StreamPersistenceOptions(BaseModel):
    """Enable persistent delivery without constructing a diagnostic trace.

    Buffer limits apply to the ready queue; one oversized event may occupy it
    alone. Display events beyond it wait in order without blocking the model,
    with adjacent deltas of one response channel merged. The producer waits for
    the reader only at batch publication and compaction request boundaries;
    ``stall_timeout_seconds`` bounds each such wait. This is not a transport
    frame limit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_buffer_events: StrictInt = Field(default=256, gt=0)
    max_buffer_bytes: StrictInt = Field(default=1_048_576, gt=0)
    ack_timeout_seconds: FiniteFloat | None = Field(default=None, gt=0)
    stall_timeout_seconds: FiniteFloat | None = Field(default=None, gt=0)
