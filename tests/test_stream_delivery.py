"""Unbounded event delivery and durable acknowledgement boundaries."""

import asyncio

import pytest

import dagent
from dagent.harness_runtime.stream_delivery import PersistenceDeliveryError, StreamDelivery
from dagent.result import ResponseStartedData, TextDeltaData
from dagent.schemas.conversation_changes import ConversationChangeBatch


def delta(text):
    return dagent.RunStreamEvent(type="response.content.delta",
        data=TextDeltaData(delta=text, response_id="response"), run_id="run")


def batch(sequence=1):
    return ConversationChangeBatch(
        schema_version=2, conversation_schema_version=None, batch_id=f"delivery:{sequence}",
        delivery_id="delivery", sequence=sequence, conversation_id=None, run_id="run",
        base_revision=None, revision=None,
    )


def delivery(**options):
    value = StreamDelivery(dagent.StreamPersistenceOptions(**options))
    value.run_id = "run"
    return value


@pytest.mark.asyncio
async def test_unbounded_queue_keeps_every_event_in_order():
    value = delivery()
    events = [dagent.RunStreamEvent(type="response.started",
              data=ResponseStartedData(response_id="response"), run_id="run")]
    events.extend(delta(str(index)) for index in range(1024))
    for event in events:
        value.emit(event)
    value.finish()
    received = []
    while (event := await value.receive()) is not None:
        received.append(event)
    assert received == events
    assert value.failure is None


@pytest.mark.asyncio
async def test_batch_follows_display_events_and_waits_only_for_ack():
    value = delivery()
    events = [delta(str(index)) for index in range(512)]
    for event in events:
        value.emit(event)
    publication = asyncio.create_task(value.publish(batch()))
    await asyncio.sleep(0)
    received = [await value.receive() for _ in range(len(events) + 1)]
    assert received[:-1] == events
    assert received[-1].type == "conversation.changed"
    assert not publication.done()
    assert value.acknowledge("run", "delivery:1")
    assert await asyncio.wait_for(publication, 1) is False
    assert value.acknowledged_sequence == 1


@pytest.mark.asyncio
async def test_ack_timeout_reports_an_unconfirmed_batch():
    value = delivery(ack_timeout_seconds=.01)
    publication = asyncio.create_task(value.publish(batch()))
    assert (await value.receive()).type == "conversation.changed"
    with pytest.raises(PersistenceDeliveryError) as raised:
        await asyncio.wait_for(publication, 1)
    assert raised.value.code == "PersistenceTimeout"
    assert value.failed_batch_id == "delivery:1"
    assert value.acknowledged_sequence == 0


@pytest.mark.asyncio
async def test_close_wakes_an_idle_reader():
    value = delivery()
    reader = asyncio.create_task(value.receive())
    await asyncio.sleep(0)
    assert not reader.done()
    value.close()
    assert await asyncio.wait_for(reader, 1) is None


@pytest.mark.asyncio
async def test_close_releases_ack_wait_and_ignores_late_display_events():
    value = delivery()
    publication = asyncio.create_task(value.publish(batch()))
    assert (await value.receive()).type == "conversation.changed"
    value.close()
    value.emit(delta("ignored"))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(publication, 1)
    assert await value.receive() is None
    assert not value.acknowledge("run", "delivery:1")
    assert value.acknowledged_sequence == 0
    with pytest.raises(PersistenceDeliveryError, match="closed"):
        await value.publish(batch(2))
