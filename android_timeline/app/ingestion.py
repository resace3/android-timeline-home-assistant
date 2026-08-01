"""Batch ingestion.

The contract this module upholds:

* An event id is stored at most once, whether it arrives twice in one
  batch, twice in the same batch replayed, or in two different batches.
* Every event in the request gets an explicit verdict. A batch is never
  acknowledged wholesale.
* A rejected event does not prevent its siblings from being stored.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from . import PROTOCOL_VERSION, SERVER_VERSION
from .acknowledgements import build_acknowledgement
from .config import Settings
from .database import Database
from .models import (
    Acknowledgement,
    EventBatch,
    RawEvent,
    RejectedEvent,
    iso_utc,
    utc_now,
)

__all__ = ["IngestionResult", "ingest_batch"]

logger = logging.getLogger(__name__)

HEARTBEAT_SOURCE = "heartbeat"


@dataclass(slots=True)
class IngestionResult:
    acknowledgement: Acknowledgement
    is_replay: bool
    stored_ids: list[str]
    duplicate_ids: list[str]
    rejected_ids: list[str]


def _validate_event(event: RawEvent, settings: Settings) -> str | None:
    """Return a rejection reason, or ``None`` when the event is acceptable."""
    encoded = len(json.dumps(event.model_dump(), separators=(",", ":")).encode("utf-8"))
    if encoded > settings.max_event_bytes:
        return f"event exceeds {settings.max_event_bytes} bytes"

    now = utc_now()
    try:
        event_time = event.event_time
    except ValueError:
        return "event_time_utc is not a valid timestamp"

    if event_time > now + timedelta(seconds=settings.max_future_skew_seconds):
        return (
            "event_time_utc is too far in the future "
            f"(> {settings.max_future_skew_seconds}s ahead of server time)"
        )
    if event_time.year < 2000:
        return "event_time_utc is implausibly old"

    return None


def ingest_batch(database: Database, settings: Settings, batch: EventBatch) -> IngestionResult:
    """Store a validated batch and build its acknowledgement."""
    received_at = utc_now()

    rejected: list[RejectedEvent] = []
    to_store: dict[str, RawEvent] = {}
    intra_batch_duplicates: list[str] = []

    for event in batch.events:
        if event.device_id != batch.device_id:
            rejected.append(
                RejectedEvent(
                    event_id=event.event_id,
                    reason="event device_id does not match the batch device_id",
                )
            )
            continue

        reason = _validate_event(event, settings)
        if reason:
            rejected.append(RejectedEvent(event_id=event.event_id, reason=reason))
            continue

        # A batch that repeats an event id internally is not an error; the
        # second copy is simply the same observation. It still needs its own
        # verdict, though -- every event in the request gets one, or the
        # collector cannot tell what the server actually holds.
        if event.event_id in to_store:
            intra_batch_duplicates.append(event.event_id)
        else:
            to_store[event.event_id] = event

    rows = [
        (
            event.event_id,
            event.device_id,
            event.source,
            event.event_type,
            event.event_time_utc,
            event.collected_time_utc,
            event.timezone_offset_minutes,
            event.schema_version,
            json.dumps(sorted(set(event.quality_flags))),
            json.dumps(event.payload, sort_keys=True, separators=(",", ":")),
            iso_utc(received_at),
            batch.batch_id,
            batch.protocol_version,
        )
        for event in to_store.values()
    ]

    stored_ids, duplicate_ids = database.store_events(rows, batch.batch_id)
    # Repeats within this request are duplicates too, from the client's
    # point of view: the server holds the event, so stop sending it.
    duplicate_ids = [*duplicate_ids, *intra_batch_duplicates]

    # Heartbeats are also raw events; they are additionally projected into
    # their own table so coverage can ask "was the collector alive?" without
    # scanning the event store.
    stored_set = set(stored_ids)
    for event in to_store.values():
        if event.source == HEARTBEAT_SOURCE and event.event_id in stored_set:
            database.record_heartbeat(event.model_dump())

    is_new_batch = database.record_batch(
        {
            "batch_id": batch.batch_id,
            "device_id": batch.device_id,
            "received_at_utc": iso_utc(received_at),
            "client_created_time_utc": batch.created_time_utc,
            "collector_version": batch.collector_version,
            "protocol_version": batch.protocol_version,
            "event_count": len(batch.events),
            "stored_count": len(stored_ids),
            "duplicate_count": len(duplicate_ids),
            "rejected_count": len(rejected),
        }
    )

    database.record_acknowledgements(
        batch.batch_id,
        [(event_id, "stored", "") for event_id in stored_ids]
        + [(event_id, "duplicate", "") for event_id in duplicate_ids]
        + [(r.event_id, "rejected", r.reason) for r in rejected],
    )
    database.touch_device(batch.device_id)

    acknowledgement = build_acknowledgement(
        batch.batch_id,
        received=len(batch.events),
        stored_ids=stored_ids,
        duplicate_ids=duplicate_ids,
        rejected=rejected,
        received_time_utc=iso_utc(received_at),
    )

    logger.info(
        "ingested batch %s from %s: %d stored, %d duplicate, %d rejected%s",
        batch.batch_id,
        batch.device_id,
        len(stored_ids),
        len(duplicate_ids),
        len(rejected),
        "" if is_new_batch else " (batch replay)",
    )

    return IngestionResult(
        acknowledgement=acknowledgement,
        is_replay=not is_new_batch,
        stored_ids=stored_ids,
        duplicate_ids=duplicate_ids,
        rejected_ids=[r.event_id for r in rejected],
    )


def device_status(database: Database, device_id: str) -> dict[str, Any]:
    """Everything a client or a dashboard needs, and no credentials."""
    device = database.device(device_id)
    if device is None:
        raise KeyError(device_id)

    heartbeat = database.latest_heartbeat(device_id)
    batches = database.batches(device_id, limit=5)
    sources = database.sources(device_id)

    return {
        "device_id": device_id,
        "display_name": device.get("display_name", ""),
        "enabled": bool(device.get("enabled", 1)),
        "created_at_utc": device.get("created_at_utc"),
        "last_seen_utc": device.get("last_seen_utc"),
        "protocol_version": PROTOCOL_VERSION,
        "server_version": SERVER_VERSION,
        "total_events": database.count_events(device_id),
        "sources": sources,
        "last_batch": batches[0] if batches else None,
        "recent_batches": batches,
        "last_heartbeat": (
            {
                "event_time_utc": heartbeat["event_time_utc"],
                "received_at_utc": heartbeat["received_at_utc"],
                "collector_version": heartbeat["collector_version"],
                "queue_pending": heartbeat["queue_pending"],
                "enabled_collectors": heartbeat["enabled_collectors"],
            }
            if heartbeat
            else None
        ),
    }
