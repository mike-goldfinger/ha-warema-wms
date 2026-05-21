"""Binary sensor platform for Warema WMS integration.

Exposes a "Moving" binary sensor per blind that reflects the
moving flag from the WMS position payload (True = blind is moving).
"""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BLIND_DEVICE_TYPES, CONF_DEVICES, DOMAIN
from .coordinator import WaremaCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Warema WMS binary sensor entities from a config entry."""
    coordinator: WaremaCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[WaremaWmsMovingSensor] = []

    for device in entry.data.get(CONF_DEVICES, []):
        device_type = device.get("device_type", "20")
        if device_type not in BLIND_DEVICE_TYPES:
            continue

        snr = device.get("snr")
        snr_hex = device.get("snr_hex", "")
        device_type_str = device.get("device_type_str", "Blind")
        snr_int = int(snr) if not isinstance(snr, int) else snr

        entities.append(
            WaremaWmsMovingSensor(
                coordinator=coordinator,
                snr=snr_int,
                snr_hex=snr_hex,
                device_type_str=device_type_str,
                entry_id=entry.entry_id,
            )
        )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Warema WMS binary sensor entities", len(entities))


class WaremaWmsMovingSensor(CoordinatorEntity[WaremaCoordinator], BinarySensorEntity):
    """Binary sensor: True when the blind is currently moving."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.MOVING

    def __init__(
        self,
        coordinator: WaremaCoordinator,
        snr: int,
        snr_hex: str,
        device_type_str: str,
        entry_id: str,
    ) -> None:
        super().__init__(coordinator, context=snr)
        self._snr = snr
        self._snr_hex = snr_hex
        self._device_type_str = device_type_str
        self._entry_id = entry_id

        self._attr_name = "Moving"
        self._attr_unique_id = f"{DOMAIN}_{snr_hex}_moving"

    def _get_blind_state(self):
        """Get the current blind state from coordinator data."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self._snr)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._snr_hex)},
            name=f"{self._device_type_str} {self._snr}",
            manufacturer="Warema",
            model=self._device_type_str,
            via_device=(DOMAIN, self._entry_id),
        )

    @property
    def is_on(self) -> bool:
        """Return True if the blind is moving."""
        state = self._get_blind_state()
        return state.moving if state else False
