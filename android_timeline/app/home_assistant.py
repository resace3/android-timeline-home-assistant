"""Publishing summary entities to Home Assistant.

Only low-cardinality summaries are published: one entity per *statistic*,
never one per raw event. A phone produces tens of thousands of events a
day; turning those into entities would bloat the recorder database and make
the UI unusable, for no benefit that the MCP server does not already give.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import SERVER_VERSION
from .config import Settings
from .coverage import coverage_summary
from .database import Database
from .models import iso_utc, parse_iso_utc, utc_now
from .timeline import yesterday_bounds

__all__ = ["ENTITY_DEFINITIONS", "build_entity_states", "publish_entities"]

logger = logging.getLogger(__name__)

#: entity_id suffix -> (friendly name, unit, device class, icon)
ENTITY_DEFINITIONS: dict[str, tuple[str, str, str, str]] = {
    "last_sync": ("Android Timeline last sync", "", "timestamp", "mdi:cloud-upload"),
    "queue_status": ("Android Timeline queue", "events", "", "mdi:tray-full"),
    "events_today": ("Android Timeline events today", "events", "", "mdi:counter"),
    "data_coverage_today": (
        "Android Timeline data coverage today",
        "%",
        "",
        "mdi:chart-donut",
    ),
    "battery_mean_today": (
        "Android Timeline battery mean today",
        "%",
        "battery",
        "mdi:battery",
    ),
    "charging_minutes_today": (
        "Android Timeline charging minutes today",
        "min",
        "duration",
        "mdi:power-plug",
    ),
    "wifi_minutes_today": (
        "Android Timeline Wi-Fi minutes today",
        "min",
        "duration",
        "mdi:wifi",
    ),
}

BINARY_ENTITY_DEFINITIONS: dict[str, tuple[str, str, str]] = {
    "collector_online": (
        "Android Timeline collector online",
        "connectivity",
        "mdi:cellphone-check",
    ),
    "data_complete_today": (
        "Android Timeline data complete today",
        "problem",
        "mdi:database-check",
    ),
}


def _daily_value(
    database: Database, device_id: str, local_date: str, feature: str
) -> float | None:
    rows = database.daily_features(device_id, local_date, local_date, names=[feature])
    if not rows:
        return None
    value = rows[0]["value_numeric"]
    return float(value) if value is not None else None


def build_entity_states(
    database: Database, settings: Settings, device_id: str
) -> dict[str, dict[str, Any]]:
    """Compute the entity payloads for one device.

    Returns a mapping of full entity_id to a Home Assistant state body.
    Pure: no network, so it can be asserted on directly in tests.
    """
    start, end, local_date, tz_name = yesterday_bounds(settings.timezone)
    # "Today" for entity purposes is the most recent complete local day the
    # server has features for -- a partial day would make the numbers jump
    # around for no reason.
    summary = coverage_summary(database, device_id, start, end)
    heartbeat = database.latest_heartbeat(device_id)
    device = database.device(device_id) or {}

    online = False
    queue_pending = 0
    if heartbeat:
        age = (utc_now() - parse_iso_utc(heartbeat["event_time_utc"])).total_seconds()
        online = age < 2 * 3600
        queue_pending = int(heartbeat["queue_pending"])

    slug = device_id.replace("-", "_").replace(".", "_").replace(":", "_").lower()
    base_attributes = {
        "device_id": device_id,
        "server_version": SERVER_VERSION,
        "local_date": local_date,
        "timezone": tz_name,
        "attribution": "Collected by android-timeline-termux on your own device",
    }

    events_today = sum(
        row["event_count"]
        for row in database.coverage(device_id, iso_utc(start), iso_utc(end))
    )

    values: dict[str, Any] = {
        "last_sync": device.get("last_seen_utc") or "unknown",
        "queue_status": queue_pending,
        "events_today": events_today,
        "data_coverage_today": round(summary["coverage_proportion"] * 100, 1),
        "battery_mean_today": _daily_value(
            database, device_id, local_date, "battery_percentage_mean"
        ),
        "charging_minutes_today": _daily_value(
            database, device_id, local_date, "charging_minutes"
        ),
        "wifi_minutes_today": _daily_value(
            database, device_id, local_date, "wifi_connected_minutes"
        ),
    }

    states: dict[str, dict[str, Any]] = {}
    for suffix, (name, unit, device_class, icon) in ENTITY_DEFINITIONS.items():
        value = values.get(suffix)
        attributes: dict[str, Any] = {
            "friendly_name": name,
            "icon": icon,
            **base_attributes,
        }
        if unit:
            attributes["unit_of_measurement"] = unit
        if device_class:
            attributes["device_class"] = device_class
        states[f"sensor.android_timeline_{slug}_{suffix}"] = {
            "state": "unknown" if value is None else str(value),
            "attributes": attributes,
        }

    binary_values = {
        "collector_online": online,
        # 'problem' device class: 'on' means there IS a problem, so this is
        # inverted deliberately.
        "data_complete_today": not summary["data_complete"],
    }
    for suffix, (name, device_class, icon) in BINARY_ENTITY_DEFINITIONS.items():
        states[f"binary_sensor.android_timeline_{slug}_{suffix}"] = {
            "state": "on" if binary_values[suffix] else "off",
            "attributes": {
                "friendly_name": name,
                "device_class": device_class,
                "icon": icon,
                **base_attributes,
            },
        }

    return states


async def publish_entities(
    database: Database, settings: Settings, *, client: httpx.AsyncClient | None = None
) -> int:
    """Push summary entities to Home Assistant via the Supervisor proxy.

    Returns the number of entities written. Never raises: a Home Assistant
    hiccup must not affect ingestion, which is the part that cannot be
    retried later.
    """
    if not settings.publish_entities:
        return 0
    if not settings.supervisor_token:
        logger.debug("no SUPERVISOR_TOKEN; skipping entity publication")
        return 0

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=15.0)
    written = 0

    try:
        for device in database.devices():
            states = build_entity_states(database, settings, device["device_id"])
            for entity_id, body in states.items():
                url = f"{settings.supervisor_url}/core/api/states/{entity_id}"
                try:
                    response = await http.post(
                        url,
                        json=body,
                        headers={
                            "Authorization": f"Bearer {settings.supervisor_token}",
                            "Content-Type": "application/json",
                        },
                    )
                    if response.status_code >= 400:
                        logger.warning(
                            "Home Assistant rejected %s: HTTP %s",
                            entity_id,
                            response.status_code,
                        )
                    else:
                        written += 1
                except httpx.HTTPError as exc:
                    logger.warning("could not publish %s: %s", entity_id, exc)
    finally:
        if owns_client:
            await http.aclose()

    logger.info("published %d Home Assistant entities", written)
    return written
