"""Constants for the Android Timeline integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "android_timeline"

CONF_BASE_URL: Final = "base_url"
CONF_ADMIN_TOKEN: Final = "admin_token"  # noqa: S105 - a config key name
CONF_DEVICE_ID: Final = "device_id"

DEFAULT_BASE_URL: Final = "http://a0d7b954-android_timeline:8099"
DEFAULT_SCAN_INTERVAL: Final = timedelta(minutes=5)

#: Only low-cardinality summaries are exposed. One entity per raw event
#: would bloat the recorder for no benefit -- use the MCP server for detail.
SENSORS: Final = (
    ("last_sync", "Last sync", None, None, "mdi:cloud-upload"),
    ("queue_status", "Queue", "events", None, "mdi:tray-full"),
    ("events_today", "Events today", "events", None, "mdi:counter"),
    ("data_coverage_today", "Data coverage today", "%", None, "mdi:chart-donut"),
    ("battery_mean_today", "Battery mean today", "%", "battery", "mdi:battery"),
    ("charging_minutes_today", "Charging minutes today", "min", None, "mdi:power-plug"),
    ("wifi_minutes_today", "Wi-Fi minutes today", "min", None, "mdi:wifi"),
)

BINARY_SENSORS: Final = (
    ("collector_online", "Collector online", "connectivity", "mdi:cellphone-check"),
    ("data_complete_today", "Data incomplete today", "problem", "mdi:database-alert"),
)
