"""Building and replaying per-event acknowledgements.

Kept separate from ingestion because acknowledgement is the contract the
collector depends on: it decides what the phone is allowed to stop
retrying. Getting it wrong loses data silently.
"""

from __future__ import annotations

from collections.abc import Sequence

from . import PROTOCOL_VERSION, SERVER_VERSION
from .database import Database
from .models import (
    AcceptedEvent,
    Acknowledgement,
    AcknowledgementCounts,
    RejectedEvent,
    iso_utc,
    utc_now,
)

__all__ = ["build_acknowledgement", "stored_acknowledgement"]


def build_acknowledgement(
    batch_id: str,
    *,
    received: int,
    stored_ids: Sequence[str],
    duplicate_ids: Sequence[str],
    rejected: Sequence[RejectedEvent],
    received_time_utc: str | None = None,
) -> Acknowledgement:
    """Assemble the response for one batch.

    ``stored`` and ``duplicate`` both appear under ``accepted``: from the
    collector's point of view they mean the same thing -- the server holds
    the event, stop sending it.
    """
    accepted = [AcceptedEvent(event_id=i, status="stored") for i in stored_ids]
    accepted += [AcceptedEvent(event_id=i, status="duplicate") for i in duplicate_ids]

    return Acknowledgement(
        protocol_version=PROTOCOL_VERSION,
        batch_id=batch_id,
        server_version=SERVER_VERSION,
        received_time_utc=received_time_utc or iso_utc(utc_now()),
        accepted=accepted,
        rejected=list(rejected),
        counts=AcknowledgementCounts(
            received=received,
            stored=len(stored_ids),
            duplicate=len(duplicate_ids),
            rejected=len(rejected),
        ),
    )


def stored_acknowledgement(database: Database, batch_id: str) -> Acknowledgement | None:
    """Rebuild the acknowledgement previously issued for ``batch_id``.

    Used to answer a replayed batch consistently, and by tests asserting
    that the recorded verdicts survive a restart.
    """
    batch = database.batch(batch_id)
    if batch is None:
        return None

    rows = database.query(
        "SELECT event_id, status, reason FROM batch_event_acknowledgements "
        "WHERE batch_id = ? ORDER BY event_id",
        (batch_id,),
    )
    accepted = [
        AcceptedEvent(event_id=str(r["event_id"]), status=str(r["status"]))
        for r in rows
        if r["status"] in ("stored", "duplicate")
    ]
    rejected = [
        RejectedEvent(event_id=str(r["event_id"]), reason=str(r["reason"]))
        for r in rows
        if r["status"] == "rejected"
    ]

    return Acknowledgement(
        protocol_version=PROTOCOL_VERSION,
        batch_id=batch_id,
        server_version=SERVER_VERSION,
        received_time_utc=str(batch["received_at_utc"]),
        accepted=accepted,
        rejected=rejected,
        counts=AcknowledgementCounts(
            received=int(batch["event_count"]),
            stored=int(batch["stored_count"]),
            duplicate=int(batch["duplicate_count"]),
            rejected=int(batch["rejected_count"]),
        ),
    )
