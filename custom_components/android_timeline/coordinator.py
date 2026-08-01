"""Polling coordinator.

Reads the app's own HTTP API rather than its database: the integration and
the app may run on different hosts, and the API is the supported contract.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_ADMIN_TOKEN, CONF_BASE_URL, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class AndroidTimelineCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches the daily summary for every enrolled device."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.entry = entry
        self._base_url = str(entry.data[CONF_BASE_URL]).rstrip("/")
        self._token = str(entry.data.get(CONF_ADMIN_TOKEN, ""))
        self._session = async_get_clientsession(hass)

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _get(self, path: str, **params: Any) -> Any:
        url = f"{self._base_url}{path}"
        async with self._session.get(
            url, headers=self._headers, params=params, timeout=30
        ) as response:
            if response.status == 401 or response.status == 403:
                raise UpdateFailed("the Android Timeline app rejected the admin token")
            if response.status >= 400:
                raise UpdateFailed(f"HTTP {response.status} from {path}")
            return await response.json()

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            health = await self._get("/api/v1/health")
            devices = await self._get("/api/v1/admin/devices")
        except UpdateFailed:
            raise
        except Exception as exc:
            raise UpdateFailed(f"could not reach the app: {exc}") from exc

        summaries: dict[str, Any] = {}
        for device in devices.get("devices", []):
            device_id = device["device_id"]
            try:
                timeline = await self._get("/api/v1/timeline/yesterday", device_id=device_id)
            except UpdateFailed as exc:
                _LOGGER.warning("no timeline for %s: %s", device_id, exc)
                continue

            daily = timeline.get("daily_features", {})
            coverage = timeline.get("coverage", {})
            summaries[device_id] = {
                "device": device,
                "last_sync": device.get("last_seen_utc"),
                "queue_status": _queue_pending(timeline),
                "events_today": sum(
                    block["raw_event_total"] for block in timeline["hour_blocks"]
                ),
                "data_coverage_today": round(
                    coverage.get("coverage_proportion", 0.0) * 100, 1
                ),
                "battery_mean_today": _value(daily, "battery_percentage_mean"),
                "charging_minutes_today": _value(daily, "charging_minutes"),
                "wifi_minutes_today": _value(daily, "wifi_connected_minutes"),
                "collector_online": device.get("last_seen_utc") is not None,
                # 'problem' device class: on means there IS a problem.
                "data_complete_today": not coverage.get("data_complete", False),
                "local_date": timeline.get("local_date"),
                "timezone": timeline.get("timezone"),
            }

        return {"health": health, "devices": summaries}


def _value(daily: dict[str, Any], name: str) -> Any:
    entry = daily.get(name)
    return entry.get("value") if isinstance(entry, dict) else None


def _queue_pending(timeline: dict[str, Any]) -> int:
    for block in reversed(timeline.get("hour_blocks", [])):
        features = block.get("features", {})
        if "events_total" in features:
            return int(features["events_total"].get("value") or 0)
    return 0
