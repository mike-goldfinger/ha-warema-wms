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
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    COVER_DEVICE_TYPES,
    CONF_DEVICES,
    DOMAIN,
    SIGNAL_NEW_WEATHER_STATION,
)
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
        if device_type not in COVER_DEVICE_TYPES:
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

    # Weather stations broadcast unsolicited and are not in CONF_DEVICES, so
    # create their rain sensor dynamically on first sight (and for any station
    # that already broadcast before this platform set up).
    added_stations: set[int] = set()

    @callback
    def _add_weather_station(snr: int) -> None:
        if snr in added_stations:
            return
        added_stations.add(snr)
        async_add_entities([WaremaWeatherRainSensor(coordinator, snr, entry.entry_id)])

    for snr in coordinator.weather_data:
        _add_weather_station(snr)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{SIGNAL_NEW_WEATHER_STATION}_{entry.entry_id}",
            _add_weather_station,
        )
    )


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
        )

    @property
    def is_on(self) -> bool:
        """Return True if the blind is moving."""
        state = self._get_blind_state()
        return state.moving if state else False


class WaremaWeatherRainSensor(CoordinatorEntity[WaremaCoordinator], BinarySensorEntity):
    """Binary sensor: True when the weather station reports rain."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.MOISTURE

    def __init__(
        self,
        coordinator: WaremaCoordinator,
        snr: int,
        entry_id: str,
    ) -> None:
        super().__init__(coordinator, context=snr)
        self._snr = snr
        self._entry_id = entry_id

        self._attr_translation_key = "weather_rain"
        self._attr_icon = "mdi:weather-rainy"
        self._attr_unique_id = f"{DOMAIN}_weather_{snr}_rain"

    def _get_state(self):
        return self.coordinator.weather_data.get(self._snr)

    @property
    def device_info(self) -> DeviceInfo:
        state = self._get_state()
        snr_hex = state.snr_hex if state else ""
        return DeviceInfo(
            identifiers={(DOMAIN, f"weather_{self._snr}")},
            name=f"Weather station {self._snr}",
            manufacturer="Warema",
            model="Weather station",
            serial_number=snr_hex or None,
        )

    @property
    def available(self) -> bool:
        # rain is None when the station has no rain sensor (issue #17).
        state = self._get_state()
        return state is not None and state.rain is not None

    @property
    def is_on(self) -> bool | None:
        state = self._get_state()
        if state is None:
            return None
        return state.rain
