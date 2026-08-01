"""Summary sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AndroidTimelineEntry
from .const import DOMAIN, SENSORS
from .coordinator import AndroidTimelineCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AndroidTimelineEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for device_id in coordinator.data.get("devices", {}):
        entities.extend(
            AndroidTimelineSensor(coordinator, device_id, *definition)
            for definition in SENSORS
        )
    async_add_entities(entities)


class AndroidTimelineSensor(CoordinatorEntity[AndroidTimelineCoordinator], SensorEntity):
    """One summary statistic for one device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AndroidTimelineCoordinator,
        device_id: str,
        key: str,
        name: str,
        unit: str | None,
        device_class: str | None,
        icon: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = unit
        self._attr_unique_id = f"{DOMAIN}_{device_id}_{key}"
        if device_class:
            self._attr_device_class = device_class  # type: ignore[assignment]

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=f"Android Timeline {self._device_id}",
            manufacturer="android-timeline",
            model="Termux collector",
            configuration_url="https://github.com/resace3/android-timeline-termux",
        )

    @property
    def available(self) -> bool:
        return super().available and self._device_id in self.coordinator.data.get(
            "devices", {}
        )

    @property
    def native_value(self) -> Any:
        return self.coordinator.data["devices"][self._device_id].get(self._key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        summary = self.coordinator.data["devices"][self._device_id]
        return {
            "device_id": self._device_id,
            "local_date": summary.get("local_date"),
            "timezone": summary.get("timezone"),
        }
