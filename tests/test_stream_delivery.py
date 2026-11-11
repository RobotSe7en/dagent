"""Display events never block the producer; boundaries own every wait and failure."""

import asyncio

import pytest

import dagent
from dagent.harness_runtime.stream_delivery import PersistenceDeliveryError, StreamDelivery
from dagent.result import ResponseStartedData, TextDeltaData
from dagent.schemas.conversation_changes import ConversationChangeBatch


def delta(text, response="response", kind="response.content.delta"):
    return dagent.RunStreamEvent(type=kind, data=TextDeltaData(delta=text, response_id=response), run_id="run")


def started(response="response"):
    return dagent.RunStreamEvent(type="response.started", data=ResponseStartedData(response_id=response), run_id="run")


def batch(sequence=1, **terminal):
    return ConversationChangeBatch(
        schema_version=2, conversation_schema_version=None, batch_id=f"delivery:{sequence}",
        delivery_id="delivery", sequence=sequence, conversation_id=None, run_id="run",
        base_revision=None, revision=None, **terminal,
    )


def delivery(**options):
    value = StreamDelivery(dagent.StreamPersistenceOptions(**options))
    value.run_id = "run"
    return value


async def drain(value, count):
    return [await value.receive() for _ in range(count)]


@pytest.mark.asyncio
async def test_full_ready_queue_merges_adjacent_deltas_in_order():
    value = delivery(max_buffer_events=2)
    for event in (started(), delta("a"), delta("b"), delta("c"), delta("r", kind="response.reasoning.delta"),
                  delta("x", response="other"), delta("y", response="other")):
        value.emit(event)
    assert len(value._queue) == 2 and len(value._overflow) == 3
    received = await drain(value, 5)
    assert [event.type for event in received] == [
        "response.started", "response.content.delta", "response.content.delta",
        "response.reasoning.delta", "response.content.delta",
    ]
    assert [getattr(event.data, "delta", None) for event in received] == [None, "a", "bc", "r", "xy"]
    assert received[-1].data.response_id == "other"


@pytest.mark.asyncio
async def test_emit_never_raises_and_failures_surface_at_the_next_boundary():
    value = delivery(max_buffer_events=1)
    value.emit(started())
    for index in range(32):
        value.emit(started(f"r{index}"))
    assert value.failure is None and len(value._overflow) == 32
    value.emit(started("one_too_many"))
    assert value.failure is not None and value.failure.code == "StreamBufferOverflow"
    value.emit(delta("dropped"))
    with pytest.raises(PersistenceDeliveryError) as raised:
        await value.checkpoint()
    assert raised.value.code == "StreamBufferOverflow"
    closed = delivery()
    closed.close()
    closed.emit(delta("ignored"))
    with pytest.raises(PersistenceDeliveryError, match="closed"):
        await closed.checkpoint()


@pytest.mark.asyncio
async def test_boundary_waits_for_the_reader_and_stall_timeout_fails_it():
    value = delivery(max_buffer_events=1)
    value.emit(started())
    value.emit(delta("a"))
    waiter = asyncio.create_task(value.checkpoint())
    await asyncio.sleep(0)
    assert not waiter.done()
    await drain(value, 2)
    await asyncio.wait_for(waiter, 1)

    stalled = delivery(max_buffer_events=1, stall_timeout_seconds=0.01)
    stalled.emit(started())
    stalled.emit(delta("a"))
    with pytest.raises(PersistenceDeliveryError) as raised:
        await stalled.checkpoint()
    assert raised.value.code == "DeliveryStalled" and stalled.failure is raised.value


@pytest.mark.asyncio
async def test_batch_follows_its_deltas_and_waits_for_presentation_within_the_stall_timeout():
    value = delivery(max_buffer_events=1)
    value.emit(started())
    value.emit(delta("a"))
    value.emit(delta("b"))
    publication = asyncio.create_task(value.publish(batch()))
    await asyncio.sleep(0)
    received = await drain(value, 3)
    assert [event.type for event in received] == ["response.started", "response.content.delta", "conversation.changed"]
    assert received[1].data.delta == "ab"
    assert value.acknowledge("run", "delivery:1")
    assert await asyncio.wait_for(publication, 1) is False

    stalled = delivery(stall_timeout_seconds=0.01)
    with pytest.raises(PersistenceDeliveryError) as raised:
        await stalled.publish(batch())
    assert raised.value.code == "DeliveryStalled" and stalled.failed_batch_id == "delivery:1"


def test_stall_timeout_is_optional():
    assert dagent.StreamPersistenceOptions().stall_timeout_seconds is None
    assert dagent.StreamPersistenceOptions(stall_timeout_seconds=2.5).stall_timeout_seconds == 2.5
