"""Versioned feature engineering.

Every derived value is stored with its feature version, its window, the
event types it was computed from, and the coverage proportion of that
window. Changing a definition means bumping :data:`FEATURE_VERSION` and
recomputing into new rows -- raw events are never touched.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from . import FEATURE_VERSION
from .coverage import compute_coverage
from .database import Database
from .models import floor_hour, hour_range, iso_utc, parse_iso_utc, utc_now

__all__ = [
    "FEATURE_DEFINITIONS",
    "FeatureDefinition",
    "compute_daily_features",
    "compute_hourly_features",
    "register_feature_definitions",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    name: str
    description: str
    unit: str
    source_event_types: tuple[str, ...]
    #: How the daily value is derived from the hourly values.
    daily_aggregation: str = "sum"


FEATURE_DEFINITIONS: tuple[FeatureDefinition, ...] = (
    FeatureDefinition(
        "battery_percentage_mean",
        "Mean battery charge level across samples in the window.",
        "percent",
        ("battery_sample",),
        daily_aggregation="mean",
    ),
    FeatureDefinition(
        "charging_minutes",
        "Minutes in the window during which the device reported charging.",
        "minutes",
        ("battery_sample",),
    ),
    FeatureDefinition(
        "wifi_connected_minutes",
        "Minutes in the window during which Wi-Fi reported a connection.",
        "minutes",
        ("wifi_sample",),
    ),
    FeatureDefinition(
        "location_observation_coverage",
        "Proportion of the window for which at least one location fix exists.",
        "proportion",
        ("location_sample", "location_disabled"),
        daily_aggregation="mean",
    ),
    FeatureDefinition(
        "movement_summary",
        "Mean accelerometer magnitude across sensor summaries in the window.",
        "m/s^2",
        ("sensor_summary",),
        daily_aggregation="mean",
    ),
    FeatureDefinition(
        "calls_count",
        "Number of call records in the window.",
        "count",
        ("call_record",),
    ),
    FeatureDefinition(
        "sms_count",
        "Number of SMS records in the window.",
        "count",
        ("sms_record",),
    ),
    FeatureDefinition(
        "collector_heartbeat_coverage",
        "Proportion of expected heartbeats actually received in the window.",
        "proportion",
        ("collector_heartbeat",),
        daily_aggregation="mean",
    ),
    FeatureDefinition(
        "events_total",
        "Total raw events in the window, with a per-source breakdown.",
        "count",
        ("*",),
    ),
    FeatureDefinition(
        "missing_data_flag",
        "1.0 when the window contains no events at all, otherwise 0.0.",
        "boolean",
        ("*",),
        daily_aggregation="sum",
    ),
)

_BY_NAME = {definition.name: definition for definition in FEATURE_DEFINITIONS}


def register_feature_definitions(database: Database) -> None:
    for definition in FEATURE_DEFINITIONS:
        database.upsert_feature_definition(
            definition.name,
            FEATURE_VERSION,
            definition.description,
            definition.unit,
            definition.source_event_types,
        )


# ----------------------------------------------------------------------
# hourly
# ----------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _sample_minutes(count: int, window_minutes: int, expected_samples: int) -> float:
    """Convert a count of positive samples into minutes of the window.

    Sampling is periodic, so each positive sample stands for its share of
    the window. Not exact -- a state change between samples is invisible --
    which is why the value ships with a coverage proportion.
    """
    if expected_samples <= 0:
        return 0.0
    return round(min(count / expected_samples, 1.0) * window_minutes, 2)


def _hour_features(
    device_id: str,
    hour_start: datetime,
    events: list[dict[str, Any]],
    coverage_proportion: float,
    computed_at: str,
) -> list[dict[str, Any]]:
    hour_end = hour_start + timedelta(hours=1)
    by_type: dict[str, list[dict[str, Any]]] = {}
    by_source: dict[str, int] = {}
    for event in events:
        by_type.setdefault(event["event_type"], []).append(event)
        by_source[event["source"]] = by_source.get(event["source"], 0) + 1

    rows: list[dict[str, Any]] = []

    def add(
        name: str,
        value: float | None,
        *,
        text: str | None = None,
        flags: Sequence[str] = (),
    ) -> None:
        definition = _BY_NAME[name]
        rows.append(
            {
                "device_id": device_id,
                "feature_name": name,
                "feature_version": FEATURE_VERSION,
                "window_start_utc": iso_utc(hour_start),
                "window_end_utc": iso_utc(hour_end),
                "value_numeric": value,
                "value_text": text,
                "source_event_types": json.dumps(list(definition.source_event_types)),
                "coverage_proportion": round(coverage_proportion, 4),
                "quality_flags": json.dumps(sorted(set(flags))),
                "computed_at_utc": computed_at,
            }
        )

    # -- battery ------------------------------------------------------
    battery = by_type.get("battery_sample", [])
    percentages = [
        float(e["payload"]["percentage"])
        for e in battery
        if isinstance(e["payload"].get("percentage"), (int, float))
    ]
    add(
        "battery_percentage_mean",
        _mean(percentages),
        flags=() if percentages else ("partial",),
    )

    charging = sum(1 for e in battery if e["payload"].get("charging") is True)
    add("charging_minutes", _sample_minutes(charging, 60, max(len(battery), 1)))

    # -- wifi ---------------------------------------------------------
    wifi = by_type.get("wifi_sample", [])
    connected = sum(1 for e in wifi if e["payload"].get("connected") is True)
    add("wifi_connected_minutes", _sample_minutes(connected, 60, max(len(wifi), 1)))

    # -- location -----------------------------------------------------
    location = by_type.get("location_sample", [])
    available = sum(1 for e in location if e["payload"].get("location_available") is True)
    add(
        "location_observation_coverage",
        round(available / len(location), 4) if location else 0.0,
        flags=() if location else ("partial",),
    )

    # -- movement -----------------------------------------------------
    magnitudes = [
        float(e["payload"]["magnitude_mean"])
        for e in by_type.get("sensor_summary", [])
        if isinstance(e["payload"].get("magnitude_mean"), (int, float))
    ]
    add(
        "movement_summary",
        _mean(magnitudes),
        flags=("experimental",) if magnitudes else ("experimental", "partial"),
    )

    # -- communications -----------------------------------------------
    add("calls_count", float(len(by_type.get("call_record", []))))
    add("sms_count", float(len(by_type.get("sms_record", []))))

    # -- collector liveness -------------------------------------------
    heartbeats = len(by_type.get("collector_heartbeat", []))
    add("collector_heartbeat_coverage", 1.0 if heartbeats else 0.0)

    # -- totals and missingness ----------------------------------------
    add(
        "events_total",
        float(len(events)),
        text=json.dumps(dict(sorted(by_source.items()))),
    )
    add("missing_data_flag", 0.0 if events else 1.0)

    return rows


def compute_hourly_features(
    database: Database,
    device_id: str,
    start: datetime,
    end: datetime,
) -> int:
    """Compute hourly features for ``[start, end)``. Returns the row count."""
    register_feature_definitions(database)
    coverage_rows = {
        row["window_start_utc"]: row
        for row in compute_coverage(database, device_id, start, end)
    }

    events = database.events_in_window(device_id, iso_utc(start), iso_utc(end), limit=200_000)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        key = iso_utc(floor_hour(parse_iso_utc(event["event_time_utc"])))
        grouped.setdefault(key, []).append(event)

    computed_at = iso_utc(utc_now())
    rows: list[dict[str, Any]] = []
    for hour_start in hour_range(start, end):
        key = iso_utc(hour_start)
        coverage = coverage_rows.get(key, {})
        rows.extend(
            _hour_features(
                device_id,
                hour_start,
                grouped.get(key, []),
                float(coverage.get("coverage_proportion", 0.0)),
                computed_at,
            )
        )

    written = database.upsert_hourly_features(rows)
    logger.info(
        "computed %d hourly feature rows for %s (%s..%s)",
        written,
        device_id,
        iso_utc(start),
        iso_utc(end),
    )
    return written


# ----------------------------------------------------------------------
# daily
# ----------------------------------------------------------------------

_AGGREGATIONS: dict[str, Callable[[Sequence[float]], float | None]] = {
    "sum": lambda values: float(sum(values)) if values else None,
    "mean": _mean,
    "max": lambda values: max(values) if values else None,
}


def compute_daily_features(
    database: Database,
    device_id: str,
    day_start: datetime,
    *,
    local_date: str | None = None,
) -> int:
    """Roll the hourly features of one 24-hour window into daily values."""
    day_end = day_start + timedelta(days=1)
    hourly = database.hourly_features(device_id, iso_utc(day_start), iso_utc(day_end))
    if not hourly:
        compute_hourly_features(database, device_id, day_start, day_end)
        hourly = database.hourly_features(device_id, iso_utc(day_start), iso_utc(day_end))

    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in hourly:
        by_name.setdefault(row["feature_name"], []).append(row)

    date_label = local_date or day_start.date().isoformat()
    computed_at = iso_utc(utc_now())
    rows: list[dict[str, Any]] = []

    for definition in FEATURE_DEFINITIONS:
        entries = by_name.get(definition.name, [])
        values = [float(e["value_numeric"]) for e in entries if e["value_numeric"] is not None]
        aggregate = _AGGREGATIONS[definition.daily_aggregation](values)
        coverage = _mean([float(e["coverage_proportion"]) for e in entries]) or 0.0

        flags: set[str] = set()
        for entry in entries:
            flags.update(entry["quality_flags"])
        if len(values) < len(entries):
            flags.add("partial")

        rows.append(
            {
                "device_id": device_id,
                "feature_name": definition.name,
                "feature_version": FEATURE_VERSION,
                "local_date": date_label,
                "window_start_utc": iso_utc(day_start),
                "window_end_utc": iso_utc(day_end),
                "value_numeric": aggregate,
                "value_text": None,
                "source_event_types": json.dumps(list(definition.source_event_types)),
                "coverage_proportion": round(coverage, 4),
                "quality_flags": json.dumps(sorted(flags)),
                "computed_at_utc": computed_at,
            }
        )

    return database.upsert_daily_features(rows)
