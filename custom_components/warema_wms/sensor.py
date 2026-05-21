"""Sensor platform for Warema WMS integration.

Exposes per-blind sensors:
  - WMS Position  (0 = open, 100 = closed) in %
  - WMS Angle     (decoded value, same range as get_position.py output)
  - Motor SNR     (serial number as text)
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BLIND_DEVICE_TYPES, CONF_DEVICES, DOMAIN
from .coordinator import WaremaCoordinator

_LOGGER = logging.getLogger(__name__)

# (key, friendly_name, unit, icon)
_SENSOR_DEFS = [
    ("position", "WMS Position", "%", "mdi:window-shutter"),
    ("angle", "WMS Angle", None, "mdi:angle-acute"),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Warema WMS sensor entities from a config entry."""
    coordinator: WaremaCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list = []

    for device in entry.data.get(CONF_DEVICES, []):
        device_type = device.get("device_type", "20")
        if device_type not in BLIND_DEVICE_TYPES:
            continue

        snr = device.get("snr")
        snr_hex = device.get("snr_hex", "")
        device_type_str = device.get("device_type_str", "Blind")
        snr_int = int(snr) if not isinstance(snr, int) else snr

        # Position and angle sensors
        for key, name, unit, icon in _SENSOR_DEFS:
            entities.append(
                WaremaWmsSensor(
                    coordinator=coordinator,
                    snr=snr_int,
                    snr_hex=snr_hex,
                    device_type_str=device_type_str,
                    entry_id=entry.entry_id,
                    key=key,
                    name=name,
                    unit=unit,
                    icon=icon,
                )
            )

        # Motor SNR sensor (static text sensor showing the device ID)
        entities.append(
            WaremaSnrSensor(
                snr=snr_int,
                snr_hex=snr_hex,
                device_type_str=device_type_str,
                entry_id=entry.entry_id,
            )
        )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Warema WMS sensor entities", len(entities))


class WaremaWmsSensor(CoordinatorEntity[WaremaCoordinator], SensorEntity):
    """A numeric sensor that mirrors one field from the WMS position payload."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: WaremaCoordinator,
        snr: int,
        snr_hex: str,
        device_type_str: str,
        entry_id: str,
        key: str,
        name: str,
        unit: str | None,
        icon: str,
    ) -> None:
        super().__init__(coordinator, context=snr)
        self._snr = snr
        self._snr_hex = snr_hex
        self._device_type_str = device_type_str
        self._entry_id = entry_id
        self._key = key

        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_unique_id = f"{DOMAIN}_{snr_hex}_{key}"

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
    def native_value(self) -> int | None:
        """Return the sensor value from coordinator data."""
        state = self._get_blind_state()
        if not state:
            return None
        if self._key == "position":
            return state.position if state.position >= 0 else None
        return state.angle if state.position >= 0 else None


class WaremaSnrSensor(SensorEntity):
    """Text sensor that displays the motor SNR (serial number)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        snr: int,
        snr_hex: str,
        device_type_str: str,
        entry_id: str,
    ) -> None:
        self._snr = snr
        self._snr_hex = snr_hex
        self._device_type_str = device_type_str
        self._entry_id = entry_id

        self._attr_name = "Motor SNR"
        self._attr_unique_id = f"{DOMAIN}_{snr_hex}_snr"
        self._attr_icon = "mdi:identifier"

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
    def native_value(self) -> str:
        """Return the SNR as a formatted string (dec and hex)."""
        return f"{self._snr} (hex: {self._snr_hex})"
