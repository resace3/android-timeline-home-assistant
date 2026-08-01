"""The canonical synthetic day, kept byte-compatible with the collector's.

The collector repository owns the generator that CI actually runs end to
end (``android-timeline-termux/tests/fixtures/synthetic_day.py``). This is a
standalone re-implementation so this repository's tests do not need the
other checkout; a contract test in the cross-repository workflow asserts the
two produce identical events.

Nothing here resembles real personal data.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

__all__ = [
    "DAY_START",
    "DEVICE_ID",
    "GAP_END",
    "GAP_START",
    "build_synthetic_day",
]

_NAMESPACE = uuid.UUID("6f1a7a1e-6b1e-4f7d-9c0e-3f6c9b2a5d10")

DEVICE_ID = "device-test-001"
DAY_START = datetime(2026, 3, 15, 0, 0, 0, tzinfo=UTC)
DAY_END = DAY_START + timedelta(days=1)
GAP_START = DAY_START.replace(hour=2)
GAP_END = DAY_START.replace(hour=4)
CHARGE_START = DAY_START.replace(hour=6)
CHARGE_END = DAY_START.replace(hour=8)
WIFI_OFF_START = DAY_START.replace(hour=12)
WIFI_OFF_END = DAY_START.replace(hour=14)
TIMEZONE_OFFSET_MINUTES = -240


def iso_utc(value: datetime) -> str:
    value = value.astimezone(UTC)
    if value.microsecond:
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def deterministic_event_id(
    device_id: str,
    source: str,
    event_type: str,
    event_time_utc: str,
    payload: dict[str, Any] | None = None,
) -> str:
    seed = "|".join(
        [
            device_id,
            source,
            event_type,
            event_time_utc,
            json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str),
        ]
    )
    return str(uuid.uuid5(_NAMESPACE, seed))


def _in_gap(when: datetime) -> bool:
    return GAP_START <= when < GAP_END


def _event(
    source: str,
    event_type: str,
    when: datetime,
    payload: dict[str, Any],
    *,
    collected: datetime | None = None,
    quality_flags: list[str] | None = None,
) -> dict[str, Any]:
    event_time = iso_utc(when)
    return {
        "event_id": deterministic_event_id(DEVICE_ID, source, event_type, event_time, payload),
        "device_id": DEVICE_ID,
        "source": source,
        "event_type": event_type,
        "event_time_utc": event_time,
        "collected_time_utc": iso_utc(collected or when),
        "timezone_offset_minutes": TIMEZONE_OFFSET_MINUTES,
        "schema_version": 1,
        "quality_flags": sorted(set(quality_flags or [])),
        "payload": payload,
    }


def build_synthetic_day() -> dict[str, Any]:
    events: list[dict[str, Any]] = []

    battery_samples = 0
    charging_samples = 0
    cursor = DAY_START
    percentage = 88
    while cursor < DAY_END:
        if not _in_gap(cursor):
            charging = CHARGE_START <= cursor < CHARGE_END
            if charging:
                percentage = min(100, percentage + 3)
                charging_samples += 1
            else:
                percentage = max(5, percentage - 1)
            events.append(
                _event(
                    "battery",
                    "battery_sample",
                    cursor,
                    {
                        "percentage": percentage,
                        "status": "CHARGING" if charging else "DISCHARGING",
                        "plugged": "AC" if charging else None,
                        "charging": charging,
                        "health": "GOOD",
                        "temperature_celsius": 31.0 if charging else 27.5,
                    },
                )
            )
            battery_samples += 1
        cursor += timedelta(minutes=15)

    wifi_connected_samples = 0
    cursor = DAY_START
    while cursor < DAY_END:
        if not _in_gap(cursor):
            connected = not (WIFI_OFF_START <= cursor < WIFI_OFF_END)
            if connected:
                wifi_connected_samples += 1
            events.append(
                _event(
                    "wifi",
                    "wifi_sample",
                    cursor,
                    {
                        "connected": connected,
                        "ssid_pseudonym": "p_synthetic0000home" if connected else None,
                        "bssid_pseudonym": "p_synthetic0000bss" if connected else None,
                        "supplicant_state": "COMPLETED" if connected else "DISCONNECTED",
                        "rssi_dbm": -52 if connected else None,
                    },
                )
            )
        cursor += timedelta(minutes=30)

    location_samples = 0
    cursor = DAY_START
    while cursor < DAY_END:
        if not _in_gap(cursor):
            at_work = 9 <= cursor.hour < 17
            events.append(
                _event(
                    "location",
                    "location_sample",
                    cursor,
                    {
                        "location_mode": "coarse",
                        "location_available": True,
                        "place_category": (
                            "place-work-synthetic" if at_work else "place-home-synthetic"
                        ),
                        "latitude": 10.12 if at_work else 10.15,
                        "longitude": 20.57 if at_work else 20.61,
                        "precision_decimal_places": 2,
                        "accuracy_metres": 25.0,
                        "provider": "network",
                    },
                    quality_flags=["coarse", "redacted"],
                )
            )
            location_samples += 1
        cursor += timedelta(hours=1)

    movement_samples = 0
    cursor = DAY_START
    while cursor < DAY_END:
        if not _in_gap(cursor):
            moving = 8 <= cursor.hour < 20
            events.append(
                _event(
                    "sensors",
                    "sensor_summary",
                    cursor,
                    {
                        "sensor": "Synthetic Accelerometer",
                        "available": True,
                        "samples_requested": 5,
                        "samples_returned": 5,
                        "magnitude_mean": 10.42 if moving else 9.81,
                        "magnitude_min": 9.60 if moving else 9.79,
                        "magnitude_max": 12.30 if moving else 9.83,
                        "magnitude_stddev": 0.87 if moving else 0.01,
                    },
                    quality_flags=["experimental"],
                )
            )
            movement_samples += 1
        cursor += timedelta(hours=3)

    call_times = [
        DAY_START.replace(hour=9, minute=15),
        DAY_START.replace(hour=18, minute=44),
    ]
    for index, when in enumerate(call_times):
        events.append(
            _event(
                "calls",
                "call_record",
                when,
                {
                    "direction": "incoming" if index == 0 else "missed",
                    "duration_seconds": 214 if index == 0 else 0,
                    "counterparty_pseudonym": f"p_synthetic00000{index}",
                },
                quality_flags=["redacted"],
            )
        )

    sms_times = [
        DAY_START.replace(hour=10, minute=5),
        DAY_START.replace(hour=10, minute=6),
    ]
    for index, when in enumerate(sms_times):
        events.append(
            _event(
                "sms",
                "sms_record",
                when,
                {
                    "direction": "incoming" if index == 0 else "outgoing",
                    "read": True,
                    "body_length": 44,
                    "counterparty_pseudonym": "p_synthetic000000",
                    "thread_pseudonym": "p_synthetic0000th",
                },
                quality_flags=["redacted"],
            )
        )

    heartbeats = 0
    cursor = DAY_START
    while cursor < DAY_END:
        if not _in_gap(cursor):
            events.append(
                _event(
                    "heartbeat",
                    "collector_heartbeat",
                    cursor,
                    {
                        "collector_version": "0.1.0",
                        "protocol_version": 1,
                        "termux_detected": False,
                        "queue": {"pending_events": 0, "total_events": 0},
                        "enabled_collectors": [
                            "battery",
                            "calls",
                            "location",
                            "sensors",
                            "sms",
                            "wifi",
                        ],
                        "location_mode": "coarse",
                        "salt_configured": True,
                    },
                    quality_flags=["mocked"],
                )
            )
            heartbeats += 1
        cursor += timedelta(hours=1)

    late_event = _event(
        "tasker",
        "screen_on",
        DAY_START.replace(hour=5, minute=0),
        {"trigger": "synthetic-late-arrival"},
        collected=DAY_START.replace(hour=20, minute=0),
        quality_flags=["late_arrival"],
    )
    events.append(late_event)

    duplicate_source = events[0]
    events.append(json.loads(json.dumps(duplicate_source)))

    events.sort(key=lambda e: (e["event_time_utc"], e["source"], e["event_id"]))
    unique_ids = {e["event_id"] for e in events}

    return {
        "device_id": DEVICE_ID,
        "day_start_utc": iso_utc(DAY_START),
        "day_end_utc": iso_utc(DAY_END),
        "timezone_offset_minutes": TIMEZONE_OFFSET_MINUTES,
        "events": events,
        "expected": {
            "total_event_rows": len(events),
            "unique_event_ids": len(unique_ids),
            "duplicate_rows": len(events) - len(unique_ids),
            "battery_samples": battery_samples,
            "charging_samples": charging_samples,
            "wifi_samples_connected": wifi_connected_samples,
            "location_samples": location_samples,
            "movement_samples": movement_samples,
            "calls": len(call_times),
            "sms": len(sms_times),
            "heartbeats": heartbeats,
            "gap_start_utc": iso_utc(GAP_START),
            "gap_end_utc": iso_utc(GAP_END),
            "gap_hours": [2, 3],
            "charging_window_utc": [iso_utc(CHARGE_START), iso_utc(CHARGE_END)],
            "wifi_off_window_utc": [iso_utc(WIFI_OFF_START), iso_utc(WIFI_OFF_END)],
            "late_arrival_event_id": late_event["event_id"],
            "replay_event_id": duplicate_source["event_id"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="emit the synthetic day as JSON")
    parser.add_argument("--output")
    parser.add_argument("--events-only", action="store_true")
    args = parser.parse_args(argv)

    day = build_synthetic_day()
    payload: Any = day["events"] if args.events_only else day
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {len(day['events'])} events", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
