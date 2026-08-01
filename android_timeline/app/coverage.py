"""Data coverage and gap detection.

The point of this module: a flat line and a dead collector must never look
the same. Every derived number is published next to the proportion of its
window that was actually observed, so a reader can tell "nothing happened"
from "we were not watching".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from .database import Database
from .models import floor_hour, hour_range, iso_utc, parse_iso_utc, utc_now

__all__ = [
    "DEFAULT_EXPECTED_SOURCES",
    "compute_coverage",
    "expected_sources_for",
    "find_gaps",
]

#: Fallback when no heartbeat has ever been received. Battery and Wi-Fi are
#: the collectors that are enabled by default on the phone.
DEFAULT_EXPECTED_SOURCES: tuple[str, ...] = ("battery", "wifi")

#: A heartbeat is expected at least this often. The collector default is
#: 900s; allowing an hour keeps a single missed beat from reading as an
#: outage.
HEARTBEAT_INTERVAL_SECONDS = 3600


def expected_sources_for(database: Database, device_id: str) -> list[str]:
    """Which sources should be producing data, according to the device.

    Taken from the most recent heartbeat rather than from server-side
    configuration: the phone is the only thing that knows what the user
    actually enabled.
    """
    heartbeat = database.latest_heartbeat(device_id)
    if heartbeat:
        enabled = [str(s) for s in heartbeat.get("enabled_collectors") or []]
        if enabled:
            return sorted(enabled)
    return list(DEFAULT_EXPECTED_SOURCES)


def compute_coverage(
    database: Database,
    device_id: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Compute and persist hourly coverage for ``[start, end)``."""
    expected = expected_sources_for(database, device_id)
    expected_set = set(expected)
    computed_at = iso_utc(utc_now())

    events = database.events_in_window(device_id, iso_utc(start), iso_utc(end), limit=200_000)

    by_hour: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    heartbeats: dict[str, int] = {}

    for event in events:
        hour = iso_utc(floor_hour(parse_iso_utc(event["event_time_utc"])))
        by_hour.setdefault(hour, set()).add(event["source"])
        counts[hour] = counts.get(hour, 0) + 1
        if event["source"] == "heartbeat":
            heartbeats[hour] = heartbeats.get(hour, 0) + 1

    rows: list[dict[str, Any]] = []
    for hour_start in hour_range(start, end):
        key = iso_utc(hour_start)
        observed = sorted(by_hour.get(key, set()) & expected_set)
        event_count = counts.get(key, 0)
        heartbeat_count = heartbeats.get(key, 0)

        proportion = len(observed) / len(expected_set) if expected_set else 0.0
        # An hour with no events at all is missing regardless of what was
        # expected -- that is the two-hour-gap case the fixtures exercise.
        is_missing = event_count == 0

        rows.append(
            {
                "device_id": device_id,
                "window_start_utc": key,
                "window_end_utc": iso_utc(hour_start + timedelta(hours=1)),
                "expected_sources": json.dumps(expected),
                "observed_sources": json.dumps(observed),
                "event_count": event_count,
                "heartbeat_count": heartbeat_count,
                "coverage_proportion": round(proportion, 4),
                "is_missing": int(is_missing),
                "computed_at_utc": computed_at,
            }
        )

    database.upsert_coverage(rows)
    return database.coverage(device_id, iso_utc(start), iso_utc(end))


def find_gaps(
    database: Database,
    device_id: str,
    start: datetime,
    end: datetime,
    *,
    min_hours: int = 1,
) -> list[dict[str, Any]]:
    """Contiguous runs of hours with no data at all.

    Returns explicit gap records rather than an absence of rows, because an
    absence is exactly what a reader cannot distinguish from silence.
    """
    coverage = database.coverage(device_id, iso_utc(start), iso_utc(end))
    if not coverage:
        coverage = compute_coverage(database, device_id, start, end)

    gaps: list[dict[str, Any]] = []
    run_start: str | None = None
    run_end: str | None = None
    run_hours = 0

    for row in coverage:
        if row["is_missing"]:
            if run_start is None:
                run_start = row["window_start_utc"]
            run_end = row["window_end_utc"]
            run_hours += 1
        elif run_start is not None:
            if run_hours >= min_hours:
                gaps.append(
                    {
                        "start_utc": run_start,
                        "end_utc": run_end,
                        "hours": run_hours,
                        "reason": "no events received in this window",
                    }
                )
            run_start, run_end, run_hours = None, None, 0

    if run_start is not None and run_hours >= min_hours:
        gaps.append(
            {
                "start_utc": run_start,
                "end_utc": run_end,
                "hours": run_hours,
                "reason": "no events received in this window",
            }
        )

    return gaps


def coverage_summary(
    database: Database, device_id: str, start: datetime, end: datetime
) -> dict[str, Any]:
    """Aggregate coverage over a window, for entities and the timeline."""
    rows = database.coverage(device_id, iso_utc(start), iso_utc(end))
    if not rows:
        rows = compute_coverage(database, device_id, start, end)

    total = len(rows)
    missing = sum(1 for r in rows if r["is_missing"])
    mean = sum(r["coverage_proportion"] for r in rows) / total if total else 0.0

    return {
        "device_id": device_id,
        "window_start_utc": iso_utc(start),
        "window_end_utc": iso_utc(end),
        "hours_total": total,
        "hours_missing": missing,
        "hours_observed": total - missing,
        "coverage_proportion": round(mean, 4),
        "data_complete": missing == 0 and total > 0,
        "expected_sources": expected_sources_for(database, device_id),
    }
