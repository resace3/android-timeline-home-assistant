"""The Android Timeline integration.

Optional. The app can publish its own entities through the Home Assistant
API; this integration exists for installations that prefer a config entry, a
device registry entry and proper unavailability handling.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import AndroidTimelineCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

type AndroidTimelineEntry = ConfigEntry[AndroidTimelineCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: AndroidTimelineEntry) -> bool:
    """Set up Android Timeline from a config entry."""
    coordinator = AndroidTimelineCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AndroidTimelineEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded
