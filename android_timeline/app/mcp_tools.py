"""Read-only query functions behind the MCP tools.

Kept separate from the MCP wiring so they can be tested directly and reused
by the HTTP API. Every function here is a **read**: nothing in this module
writes to the database, executes a shell command, touches the filesystem or
accepts SQL.

Three rules apply to all of them:

* date ranges are bounded (:data:`MAX_RANGE_DAYS`)
* result sets are bounded and paginated (:data:`MAX_PAGE_SIZE`)
* sensitive payload fields are removed before anything is returned
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from . import FEATURE_VERSION, SERVER_VERSION
from .config import Settings
from .coverage import coverage_summary, expected_sources_for, find_gaps
from .database import Database
from .models import iso_utc, parse_iso_utc, utc_now
from .timeline import build_day_timeline, resolve_timezone, yesterday_bounds

__all__ = [
    "MAX_PAGE_SIZE",
    "MAX_RANGE_DAYS",
    "REDACTED_KEYS",
    "ToolError",
    "export_phone_window",
    "find_data_gaps",
    "get_collector_status",
    "get_daily_features",
    "get_data_coverage",
    "get_day_timeline",
    "get_hourly_features",
    "get_phone_latest",
    "list_devices",
    "list_phone_sources",
    "query_phone_events",
]

#: Widest window any single call may request.
MAX_RANGE_DAYS = 31
#: Largest page any single call may return.
MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 100

#: Removed from every payload before it leaves this module. Precise
#: coordinates and message content are exactly what an analysis client does
#: not need, and there is no parameter to switch this off.
REDACTED_KEYS = frozenset({"latitude", "longitude", "body", "contact_name", "altitude_metres"})
_REDACTION_MARKER = "<redacted by the MCP server>"


class ToolError(ValueError):
    """A tool argument was invalid.

    The message is safe to show a model: it never contains a stack trace,
    a file path or any stored value.
    """


# ----------------------------------------------------------------------
# validation helpers
# ----------------------------------------------------------------------


def _require_device(database: Database, device_id: str) -> str:
    if not isinstance(device_id, str) or not device_id.strip():
        raise ToolError("device_id is required")
    if database.device(device_id) is None:
        known = [d["device_id"] for d in database.devices()]
        raise ToolError(f"unknown device_id. Known devices: {', '.join(known) or 'none'}")
    return device_id


def _parse_time(value: str, field: str) -> datetime:
    try:
        return parse_iso_utc(value)
    except (ValueError, TypeError) as exc:
        raise ToolError(
            f"{field} must be an RFC 3339 UTC timestamp such as 2026-03-15T00:00:00Z ({exc})"
        ) from exc


def _bounded_window(start: str, end: str) -> tuple[datetime, datetime]:
    start_dt = _parse_time(start, "start_utc")
    end_dt = _parse_time(end, "end_utc")
    if end_dt <= start_dt:
        raise ToolError("end_utc must be after start_utc")
    if end_dt - start_dt > timedelta(days=MAX_RANGE_DAYS):
        raise ToolError(
            f"window is too wide: at most {MAX_RANGE_DAYS} days may be requested in one call"
        )
    return start_dt, end_dt


def _bounded_page(limit: int, offset: int) -> tuple[int, int]:
    try:
        limit = int(limit)
        offset = int(offset)
    except (TypeError, ValueError) as exc:
        raise ToolError("limit and offset must be integers") from exc
    if limit < 1:
        raise ToolError("limit must be at least 1")
    if limit > MAX_PAGE_SIZE:
        raise ToolError(f"limit must not exceed {MAX_PAGE_SIZE}")
    if offset < 0:
        raise ToolError("offset must not be negative")
    return limit, offset


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip sensitive keys, recursively, marking that it happened."""
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if key in REDACTED_KEYS:
            cleaned[key] = _REDACTION_MARKER
        elif isinstance(value, dict):
            cleaned[key] = _redact(value)
        else:
            cleaned[key] = value
    return cleaned


def _clean_event(event: dict[str, Any]) -> dict[str, Any]:
    result = dict(event)
    result["payload"] = _redact(event.get("payload") or {})
    return result


def _envelope(**extra: Any) -> dict[str, Any]:
    return {
        "server_version": SERVER_VERSION,
        "feature_version": FEATURE_VERSION,
        "generated_at_utc": iso_utc(utc_now()),
        **extra,
    }


# ----------------------------------------------------------------------
# tools
# ----------------------------------------------------------------------


def list_devices(database: Database) -> dict[str, Any]:
    """Every enrolled device, with counts. No tokens, ever."""
    devices = []
    for device in database.devices():
        devices.append(
            {
                "device_id": device["device_id"],
                "display_name": device["display_name"],
                "enabled": bool(device["enabled"]),
                "created_at_utc": device["created_at_utc"],
                "last_seen_utc": device["last_seen_utc"],
                "total_events": database.count_events(device["device_id"]),
            }
        )
    return _envelope(devices=devices, count=len(devices))


def list_phone_sources(database: Database, device_id: str | None = None) -> dict[str, Any]:
    """Which sources have produced data, and over what period."""
    if device_id:
        _require_device(database, device_id)
    return _envelope(
        device_id=device_id,
        sources=database.sources(device_id),
        expected_sources=(expected_sources_for(database, device_id) if device_id else []),
    )


def get_phone_latest(database: Database, device_id: str, source: str) -> dict[str, Any]:
    """The most recent event for one source."""
    _require_device(database, device_id)
    if not isinstance(source, str) or not source:
        raise ToolError("source is required")

    event = database.latest_event(device_id, source)
    if event is None:
        known = [s["source"] for s in database.sources(device_id)]
        return _envelope(
            device_id=device_id,
            source=source,
            event=None,
            note=(
                f"no events stored for source '{source}'. "
                f"Available: {', '.join(known) or 'none'}"
            ),
        )
    return _envelope(device_id=device_id, source=source, event=_clean_event(event))


def query_phone_events(
    database: Database,
    device_id: str,
    start_utc: str,
    end_utc: str,
    sources: list[str] | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> dict[str, Any]:
    """Raw events in a bounded window, paginated and redacted."""
    _require_device(database, device_id)
    start, end = _bounded_window(start_utc, end_utc)
    limit, offset = _bounded_page(limit, offset)

    if sources is not None and not isinstance(sources, list):
        raise ToolError("sources must be a list of source names")

    # One extra row tells us whether another page exists without counting.
    events = database.events_in_window(
        device_id,
        iso_utc(start),
        iso_utc(end),
        sources=sources,
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(events) > limit
    page = [_clean_event(e) for e in events[:limit]]

    return _envelope(
        device_id=device_id,
        window_start_utc=iso_utc(start),
        window_end_utc=iso_utc(end),
        timezone="UTC",
        sources=sources,
        events=page,
        count=len(page),
        limit=limit,
        offset=offset,
        has_more=has_more,
        next_offset=offset + limit if has_more else None,
    )


def get_day_timeline(
    database: Database,
    settings: Settings,
    device_id: str,
    local_date: str | None = None,
) -> dict[str, Any]:
    """A full day of hour blocks, features, coverage and gaps."""
    _require_device(database, device_id)
    tzinfo, resolved = resolve_timezone(settings.timezone)

    if local_date is None:
        start, end, date_label, resolved = yesterday_bounds(settings.timezone)
    else:
        try:
            day = datetime.fromisoformat(local_date).date()
        except (TypeError, ValueError) as exc:
            raise ToolError("local_date must be an ISO date, e.g. 2026-03-15") from exc
        start_local = datetime(day.year, day.month, day.day, tzinfo=tzinfo)
        start = start_local.astimezone(UTC)
        end = start + timedelta(days=1)
        date_label = day.isoformat()

    return _envelope(
        **build_day_timeline(
            database,
            device_id,
            start,
            end,
            local_date=date_label,
            tz_name=resolved,
        )
    )


def get_hourly_features(
    database: Database,
    device_id: str,
    start_utc: str,
    end_utc: str,
    feature_names: list[str] | None = None,
    limit: int = MAX_PAGE_SIZE,
) -> dict[str, Any]:
    """Stored hourly features for a bounded window."""
    _require_device(database, device_id)
    start, end = _bounded_window(start_utc, end_utc)
    limit, _ = _bounded_page(limit, 0)

    rows = database.hourly_features(
        device_id, iso_utc(start), iso_utc(end), names=feature_names
    )
    return _envelope(
        device_id=device_id,
        window_start_utc=iso_utc(start),
        window_end_utc=iso_utc(end),
        timezone="UTC",
        features=rows[:limit],
        count=min(len(rows), limit),
        truncated=len(rows) > limit,
    )


def get_daily_features(
    database: Database,
    device_id: str,
    start_date: str,
    end_date: str,
    feature_names: list[str] | None = None,
) -> dict[str, Any]:
    """Stored daily features between two ISO dates, inclusive."""
    _require_device(database, device_id)
    try:
        first = datetime.fromisoformat(start_date).date()
        last = datetime.fromisoformat(end_date).date()
    except (TypeError, ValueError) as exc:
        raise ToolError("start_date and end_date must be ISO dates") from exc
    if last < first:
        raise ToolError("end_date must not be before start_date")
    if (last - first).days > MAX_RANGE_DAYS:
        raise ToolError(f"at most {MAX_RANGE_DAYS} days may be requested in one call")

    rows = database.daily_features(
        device_id, first.isoformat(), last.isoformat(), names=feature_names
    )
    return _envelope(
        device_id=device_id,
        start_date=first.isoformat(),
        end_date=last.isoformat(),
        features=rows,
        count=len(rows),
    )


def get_data_coverage(
    database: Database, device_id: str, start_utc: str, end_utc: str
) -> dict[str, Any]:
    """Hourly coverage plus a summary, so absence is explicit."""
    _require_device(database, device_id)
    start, end = _bounded_window(start_utc, end_utc)
    return _envelope(
        summary=coverage_summary(database, device_id, start, end),
        hours=database.coverage(device_id, iso_utc(start), iso_utc(end)),
    )


def find_data_gaps(
    database: Database,
    device_id: str,
    start_utc: str,
    end_utc: str,
    min_hours: int = 1,
) -> dict[str, Any]:
    """Contiguous windows with no data at all."""
    _require_device(database, device_id)
    start, end = _bounded_window(start_utc, end_utc)
    try:
        min_hours = int(min_hours)
    except (TypeError, ValueError) as exc:
        raise ToolError("min_hours must be an integer") from exc
    if min_hours < 1:
        raise ToolError("min_hours must be at least 1")

    gaps = find_gaps(database, device_id, start, end, min_hours=min_hours)
    return _envelope(
        device_id=device_id,
        window_start_utc=iso_utc(start),
        window_end_utc=iso_utc(end),
        min_hours=min_hours,
        gaps=gaps,
        gap_count=len(gaps),
    )


def get_collector_status(database: Database, device_id: str) -> dict[str, Any]:
    """Liveness: last heartbeat, queue depth, enabled collectors."""
    _require_device(database, device_id)
    heartbeat = database.latest_heartbeat(device_id)
    device = database.device(device_id) or {}

    if heartbeat is None:
        return _envelope(
            device_id=device_id,
            online=False,
            note="no heartbeat has ever been received from this device",
            last_seen_utc=device.get("last_seen_utc"),
        )

    age_seconds = (utc_now() - parse_iso_utc(heartbeat["event_time_utc"])).total_seconds()
    payload = heartbeat.get("payload") or {}

    return _envelope(
        device_id=device_id,
        online=age_seconds < 2 * 3600,
        last_heartbeat_utc=heartbeat["event_time_utc"],
        heartbeat_age_seconds=int(age_seconds),
        collector_version=heartbeat["collector_version"],
        queue_pending=heartbeat["queue_pending"],
        enabled_collectors=heartbeat["enabled_collectors"],
        collectors=payload.get("collectors", {}),
        last_successful_upload_utc=payload.get("last_successful_upload_utc"),
        last_seen_utc=device.get("last_seen_utc"),
    )


def export_phone_window(
    database: Database,
    device_id: str,
    start_utc: str,
    end_utc: str,
    limit: int = MAX_PAGE_SIZE,
    offset: int = 0,
) -> dict[str, Any]:
    """Bulk export of a bounded window: events, features and coverage."""
    _require_device(database, device_id)
    start, end = _bounded_window(start_utc, end_utc)
    limit, offset = _bounded_page(limit, offset)

    events = database.events_in_window(
        device_id, iso_utc(start), iso_utc(end), limit=limit + 1, offset=offset
    )
    has_more = len(events) > limit

    return _envelope(
        device_id=device_id,
        window_start_utc=iso_utc(start),
        window_end_utc=iso_utc(end),
        timezone="UTC",
        events=[_clean_event(e) for e in events[:limit]],
        count=min(len(events), limit),
        limit=limit,
        offset=offset,
        has_more=has_more,
        next_offset=offset + limit if has_more else None,
        hourly_features=database.hourly_features(device_id, iso_utc(start), iso_utc(end)),
        coverage=database.coverage(device_id, iso_utc(start), iso_utc(end)),
        redaction_note=(
            "Precise coordinates, message bodies and contact names are "
            "removed by the MCP server and cannot be requested through it."
        ),
    )
