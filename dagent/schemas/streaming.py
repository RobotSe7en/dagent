"""Options for explicitly acknowledged ToolLoop streams."""

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictInt


class StreamPersistenceOptions(BaseModel):
    """Enable persistent delivery without constructing a diagnostic trace.

    Limits apply to the ready queue. One producer emission group and one
    oversized event may be in flight. This is not a transport frame limit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_buffer_events: StrictInt = Field(default=256, gt=0)
    max_buffer_bytes: StrictInt = Field(default=1_048_576, gt=0)
    ack_timeout_seconds: FiniteFloat | None = Field(default=None, gt=0)
