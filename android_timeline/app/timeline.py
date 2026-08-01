"""Day timeline assembly.

Shapes the data a future timeline UI would need, and -- just as
importantly -- says explicitly where the data is *absent*. Hours with no
observations appear in the response as blocks marked missing, never as
gaps in the array.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import FEATURE_VERSION, PROTOCOL_VERSION, SERVER_VERSION
from .coverage import coverage_summary, expected_sources_for, find_gaps
from .database import Database
from .features import compute_daily_features, compute_hourly_features
from .models import floor_hour, hour_range, iso_utc, parse_iso_utc, utc_now

__all__ = ["build_day_timeline", "resolve_timezone", "yesterday_bounds"]


def resolve_timezone(name: str) -> tuple[Any, str]:
    """Resolve an IANA name, falling back to UTC rather than failing.

    A bad timezone in configuration must not take the whole app down, but
    the caller has to know a fallback happened -- so the resolved name is
    returned and echoed in every response.
    """
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return UTC, "UTC"


def yesterday_bounds(
    tz_name: str, *, now: datetime | None = None
) -> tuple[datetime, datetime, str, str]:
    """UTC bounds of 'yesterday' in the configured timezone."""
    tzinfo, resolved = resolve_timezone(tz_name)
    moment = (now or utc_now()).astimezone(tzinfo)
    local_midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = local_midnight - timedelta(days=1)
    end_local = local_midnight
    return (
        start_local.astimezone(UTC),
        end_local.astimezone(UTC),
        start_local.date().isoformat(),
        resolved,
    )


def build_day_timeline(
    database: Database,
    device_id: str,
    start: datetime,
    end: datetime,
    *,
    local_date: str,
    tz_name: str,
    recompute: bool = True,
    include_raw_counts: bool = True,
) -> dict[str, Any]:
    """Assemble one day of hour blocks, features, coverage and provenance."""
    if recompute:
        compute_hourly_features(database, device_id, start, end)
        compute_daily_features(database, device_id, start, local_date=local_date)

    hourly = database.hourly_features(device_id, iso_utc(start), iso_utc(end))
    features_by_hour: dict[str, dict[str, Any]] = {}
    flags_by_hour: dict[str, set[str]] = {}
    for row in hourly:
        bucket = features_by_hour.setdefault(row["window_start_utc"], {})
        bucket[row["feature_name"]] = {
            "value": row["value_numeric"],
            "text": row["value_text"],
            "feature_version": row["feature_version"],
            "coverage_proportion": row["coverage_proportion"],
            "source_event_types": row["source_event_types"],
        }
        flags_by_hour.setdefault(row["window_start_utc"], set()).update(row["quality_flags"])

    coverage_rows = {
        row["window_start_utc"]: row
        for row in database.coverage(device_id, iso_utc(start), iso_utc(end))
    }

    counts_by_hour: dict[str, dict[str, int]] = {}
    if include_raw_counts:
        for event in database.events_in_window(
            device_id, iso_utc(start), iso_utc(end), limit=200_000
        ):
            key = iso_utc(floor_hour(parse_iso_utc(event["event_time_utc"])))
            bucket = counts_by_hour.setdefault(key, {})
            bucket[event["source"]] = bucket.get(event["source"], 0) + 1

    tzinfo, resolved_tz = resolve_timezone(tz_name)

    blocks: list[dict[str, Any]] = []
    for hour_start in hour_range(start, end):
        key = iso_utc(hour_start)
        coverage = coverage_rows.get(key, {})
        raw_counts = counts_by_hour.get(key, {})
        blocks.append(
            {
                "hour_start_utc": key,
                "hour_end_utc": iso_utc(hour_start + timedelta(hours=1)),
                "hour_start_local": hour_start.astimezone(tzinfo).isoformat(),
                "features": features_by_hour.get(key, {}),
                "raw_event_counts": raw_counts,
                "raw_event_total": sum(raw_counts.values()),
                "coverage_proportion": coverage.get("coverage_proportion", 0.0),
                "observed_sources": coverage.get("observed_sources", []),
                "expected_sources": coverage.get("expected_sources", []),
                "is_missing": bool(coverage.get("is_missing", True)),
                "quality_flags": sorted(flags_by_hour.get(key, set())),
            }
        )

    daily = database.daily_features(device_id, local_date, local_date)
    summary = coverage_summary(database, device_id, start, end)
    gaps = find_gaps(database, device_id, start, end)

    return {
        "device_id": device_id,
        "local_date": local_date,
        "timezone": resolved_tz,
        "timezone_requested": tz_name,
        "window_start_utc": iso_utc(start),
        "window_end_utc": iso_utc(end),
        "hour_blocks": blocks,
        "daily_features": {
            row["feature_name"]: {
                "value": row["value_numeric"],
                "feature_version": row["feature_version"],
                "coverage_proportion": row["coverage_proportion"],
                "quality_flags": row["quality_flags"],
                "source_event_types": row["source_event_types"],
            }
            for row in daily
        },
        "coverage": summary,
        "gaps": gaps,
        "missingness": {
            "hours_missing": summary["hours_missing"],
            "hours_total": summary["hours_total"],
            "gap_count": len(gaps),
            "data_complete": summary["data_complete"],
        },
        "provenance": {
            "server_version": SERVER_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "feature_version": FEATURE_VERSION,
            "expected_sources": expected_sources_for(database, device_id),
            "generated_at_utc": iso_utc(utc_now()),
            "raw_events_are_immutable": True,
        },
    }
