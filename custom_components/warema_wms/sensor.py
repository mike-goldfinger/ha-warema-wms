"""Sensor platform for Warema WMS integration.

Exposes per-blind sensors:
  - WMS Position  (0 = open, 100 = closed) in %
  - WMS Angle     (decoded value, same range as get_position.py output)
  - Motor SNR     (serial number as text)
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    LIGHT_LUX,
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    COVER_DEVICE_TYPES,
    CONF_DEVICES,
    DOMAIN,
    SIGNAL_NEW_WEATHER_STATION,
    SUPPORTED_DEVICE_TYPES,
)
from .coordinator import WaremaCoordinator

_LOGGER = logging.getLogger(__name__)

# (key, friendly_name, unit, icon)
_SENSOR_DEFS = [
    ("position", "WMS Position", "%", "mdi:window-shutter"),
    ("angle", "WMS Angle", None, "mdi:angle-acute"),
]

# (attr, name, icon) for read-only motor info sensors (Block 81, diagnostic).
_MOTOR_INFO_SENSOR_DEFS = [
    ("software_version", "Softwareversion", "mdi:chip"),
    ("device_type_name", "Gerätetyp", "mdi:cog"),
]

# (attr, translation_key, unit, device_class, icon) for weather station readings.
_WEATHER_SENSOR_DEFS = [
    (
        "temp",
        "weather_temperature",
        UnitOfTemperature.CELSIUS,
        SensorDeviceClass.TEMPERATURE,
        "mdi:thermometer",
    ),
    (
        "wind",
        "weather_wind_speed",
        UnitOfSpeed.METERS_PER_SECOND,
        SensorDeviceClass.WIND_SPEED,
        "mdi:weather-windy",
    ),
    (
        "lumen",
        "weather_brightness",
        LIGHT_LUX,
        SensorDeviceClass.ILLUMINANCE,
        "mdi:white-balance-sunny",
    ),
    (
        "battery",
        "weather_battery",
        PERCENTAGE,
        SensorDeviceClass.BATTERY,
        None,  # let HA pick the level-dependent battery icon
    ),
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
        if device_type not in SUPPORTED_DEVICE_TYPES:
            continue

        snr = device.get("snr")
        snr_hex = device.get("snr_hex", "")
        device_type_str = device.get("device_type_str", "Blind")
        snr_int = int(snr) if not isinstance(snr, int) else snr

        # Product type diagnostic - created for every supported device, since
        # it is the field we need to identify what a device actually is.
        entities.append(
            WaremaProductTypeSensor(
                snr=snr_int,
                snr_hex=snr_hex,
                device_type=device_type,
                device_type_str=device_type_str,
                product_type=device.get("product_type"),
                entry_id=entry.entry_id,
            )
        )

        # The remaining sensors describe a cover (position, angle, valances).
        if device_type not in COVER_DEVICE_TYPES:
            continue

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

        # Diagnostic sensors for firmware / hardware info (Block 81)
        for attr, name, icon in _MOTOR_INFO_SENSOR_DEFS:
            entities.append(
                WaremaMotorInfoSensor(
                    coordinator=coordinator,
                    snr=snr_int,
                    snr_hex=snr_hex,
                    device_type_str=device_type_str,
                    entry_id=entry.entry_id,
                    attr=attr,
                    name=name,
                    icon=icon,
                )
            )

        # Valance position sensors (read-only diagnostic)
        for valance_num in (1, 2):
            entities.append(
                WaremaValanceSensor(
                    coordinator=coordinator,
                    snr=snr_int,
                    snr_hex=snr_hex,
                    device_type_str=device_type_str,
                    entry_id=entry.entry_id,
                    valance_num=valance_num,
                )
            )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Warema WMS sensor entities", len(entities))

    # Weather stations are not part of CONF_DEVICES: they broadcast
    # unsolicited. Create their sensors dynamically the first time a station is
    # seen, and cover any station that already broadcast before this platform
    # finished setting up.
    added_stations: set[int] = set()

    @callback
    def _add_weather_station(snr: int) -> None:
        if snr in added_stations:
            return
        added_stations.add(snr)
        async_add_entities(
            WaremaWeatherSensor(coordinator, snr, entry.entry_id, *defn)
            for defn in _WEATHER_SENSOR_DEFS
        )

    for snr in coordinator.weather_data:
        _add_weather_station(snr)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{SIGNAL_NEW_WEATHER_STATION}_{entry.entry_id}",
            _add_weather_station,
        )
    )


class WaremaWmsSensor(
    CoordinatorEntity[WaremaCoordinator], SensorEntity, RestoreEntity
):
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
        self._cached_value: int | None = None

        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_unique_id = f"{DOMAIN}_{snr_hex}_{key}"

    async def async_added_to_hass(self) -> None:
        """Restore last known value after HA restart."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                try:
                    self._cached_value = int(float(last_state.state))
                except (ValueError, TypeError):
                    pass

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update cached value whenever coordinator delivers a valid reading."""
        state = self._get_blind_state()
        if state and state.position >= 0:
            self._cached_value = (
                state.position if self._key == "position" else state.angle
            )
        super()._handle_coordinator_update()

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
    def native_value(self) -> int | None:
        """Return live coordinator value, or last cached value if unavailable."""
        state = self._get_blind_state()
        if state and state.position >= 0:
            return state.position if self._key == "position" else state.angle
        return self._cached_value


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
        )

    @property
    def native_value(self) -> str:
        """Return the SNR as a formatted string (dec and hex)."""
        return f"{self._snr} (hex: {self._snr_hex})"


class WaremaWeatherSensor(CoordinatorEntity[WaremaCoordinator], SensorEntity):
    """A numeric sensor mirroring one field of a WMS weather broadcast."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: WaremaCoordinator,
        snr: int,
        entry_id: str,
        attr: str,
        translation_key: str,
        unit: str,
        device_class: SensorDeviceClass,
        icon: str | None,
    ) -> None:
        super().__init__(coordinator, context=snr)
        self._snr = snr
        self._entry_id = entry_id
        self._field = attr

        self._attr_translation_key = translation_key
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_icon = icon
        self._attr_unique_id = f"{DOMAIN}_weather_{snr}_{attr}"
        if device_class is SensorDeviceClass.BATTERY:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

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
        # A field that decodes to None means the station has no such sensor
        # (0xFF on the wire). Show the entity as unavailable instead of a
        # bogus reading such as 92.5 °C (issue #17).
        state = self._get_state()
        return state is not None and getattr(state, self._field) is not None

    @property
    def native_value(self) -> float | int | None:
        state = self._get_state()
        if state is None:
            return None
        return getattr(state, self._field)


class WaremaMotorInfoSensor(CoordinatorEntity[WaremaCoordinator], SensorEntity):
    """Diagnostic sensor exposing a read-only firmware/hardware field (Block 81)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: WaremaCoordinator,
        snr: int,
        snr_hex: str,
        device_type_str: str,
        entry_id: str,
        attr: str,
        name: str,
        icon: str,
    ) -> None:
        super().__init__(coordinator, context=snr)
        self._snr = snr
        self._snr_hex = snr_hex
        self._device_type_str = device_type_str
        self._entry_id = entry_id
        self._attr_key = attr

        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{DOMAIN}_{snr_hex}_{attr}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._snr_hex)},
            name=self._device_type_str,
            manufacturer="Warema",
        )

    @property
    def available(self) -> bool:
        return self._snr in self.coordinator.motor_params

    @property
    def native_value(self) -> str | None:
        params = self.coordinator.motor_params.get(self._snr)
        if params is None:
            return None
        return getattr(params, self._attr_key, None)


class WaremaProductTypeSensor(SensorEntity):
    """Diagnostic sensor showing the product type reported by the device.

    The product type (Block 37) decides how the device is driven: whether it
    has a position axis, whether its louvres tilt, and which slat angle range
    applies. Exposing it makes that visible without reading the debug log.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "product_type"
    _attr_icon = "mdi:information-outline"
    _attr_should_poll = False

    def __init__(
        self,
        snr: int,
        snr_hex: str,
        device_type: str,
        device_type_str: str,
        product_type: int | None,
        entry_id: str,
    ) -> None:
        self._snr = snr
        self._snr_hex = snr_hex
        self._device_type = device_type
        self._device_type_str = device_type_str
        self._product_type = product_type
        self._entry_id = entry_id
        self._attr_unique_id = f"{DOMAIN}_{snr_hex}_product_type"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._snr_hex)},
            name=self._device_type_str,
            manufacturer="Warema",
        )

    @property
    def native_value(self) -> str:
        """Return e.g. '28 SlatRoofL70', or 'unknown' when not read yet."""
        from .pywarema.protocol import product_type_name

        if self._product_type is None:
            return "unknown"
        return f"{self._product_type} {product_type_name(self._product_type)}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw identifying fields for support requests."""
        return {
            "device_type": self._device_type,
            "device_type_str": self._device_type_str,
            "product_type": self._product_type,
            "snr": self._snr,
            "snr_hex": self._snr_hex,
        }


class WaremaValanceSensor(CoordinatorEntity[WaremaCoordinator], SensorEntity):
    """Diagnostic sensor exposing valance position (read-only, 0-100 %)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: WaremaCoordinator,
        snr: int,
        snr_hex: str,
        device_type_str: str,
        entry_id: str,
        valance_num: int,
    ) -> None:
        super().__init__(coordinator, context=snr)
        self._snr = snr
        self._snr_hex = snr_hex
        self._device_type_str = device_type_str
        self._entry_id = entry_id
        self._valance_num = valance_num

        self._attr_name = f"Valance {valance_num}"
        self._attr_icon = "mdi:window-shade"
        self._attr_unique_id = f"{DOMAIN}_{snr_hex}_valance_{valance_num}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._snr_hex)},
            name=self._device_type_str,
            manufacturer="Warema",
        )

    @property
    def native_value(self) -> int | None:
        """Return valance position (0-100 %) or None if not present/available."""
        if not self.coordinator.data:
            return None
        state = self.coordinator.data.get(self._snr)
        if state is None:
            return None
        if self._valance_num == 1:
            return state.valance_1
        elif self._valance_num == 2:
            return state.valance_2
        return None
