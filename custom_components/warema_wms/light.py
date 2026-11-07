"""Light platform for the Warema WMS integration.

Exposes WMS dimming and switching actuators as light entities.

A WMS light is always a stand-alone actuator with its own serial number - it is
never a sub-channel of a motor - and it answers the same broadcast scan as any
other device. Its brightness is carried in the same state byte and with the same
encoding a motor uses for its position (percent * 2), and it is driven by the
same command.

A switching actuator (plug receiver, type 24) uses the same command and state
byte but knows only two levels: 100 is on, 0 is off. It is exposed as an on/off
light without brightness.

Brightness only: the protocol carries no colour information.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_DEVICES,
    DOMAIN,
    LIGHT_DEVICE_TYPES,
    is_switch_light_device,
)
from .coordinator import WaremaCoordinator

_LOGGER = logging.getLogger(__name__)

# Brightness used for "turn on" when the device has no previous level to
# restore (e.g. first command after a restart).
DEFAULT_ON_LEVEL = 100


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Warema WMS light entities from a config entry."""
    coordinator: WaremaCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[WaremaLight] = []

    for device in entry.data.get(CONF_DEVICES, []):
        device_type = device.get("device_type", "")
        if device_type not in LIGHT_DEVICE_TYPES:
            continue

        snr = device.get("snr")
        if snr is None:
            continue
        snr_int = int(snr) if not isinstance(snr, int) else snr
        device_type_str = device.get("device_type_str", "Dimmer")

        entities.append(
            WaremaLight(
                coordinator=coordinator,
                snr=snr_int,
                snr_hex=device.get("snr_hex", ""),
                name=f"{device_type_str} {snr_int}",
                device_type_str=device_type_str,
                entry_id=entry.entry_id,
                product_type=device.get("product_type"),
                switch_only=is_switch_light_device(device_type),
            )
        )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Warema WMS light entities", len(entities))


def _wms_level_to_ha_brightness(level: int) -> int:
    """Convert a WMS level (0-100 %) to HA brightness (0-255)."""
    return round(max(0, min(100, level)) * 255 / 100)


def _ha_brightness_to_wms_level(brightness: int) -> int:
    """Convert HA brightness (0-255) to a WMS level (0-100 %)."""
    return round(max(0, min(255, brightness)) * 100 / 255)


class WaremaLight(CoordinatorEntity[WaremaCoordinator], LightEntity):
    """A WMS dimming actuator as a dimmable light, or a switching one as on/off."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WaremaCoordinator,
        snr: int,
        snr_hex: str,
        name: str,
        device_type_str: str,
        entry_id: str,
        product_type: int | None = None,
        switch_only: bool = False,
    ) -> None:
        """Initialize the light."""
        super().__init__(coordinator, context=snr)
        self._snr = snr
        self._snr_hex = snr_hex
        self._device_type_str = device_type_str
        self._entry_id = entry_id
        self._product_type = product_type
        self._switch_only = switch_only
        # Brightness to restore on "turn on" without an explicit brightness.
        self._last_level: int | None = None

        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{snr_hex}"
        if switch_only:
            self._attr_color_mode = ColorMode.ONOFF
            self._attr_supported_color_modes = {ColorMode.ONOFF}
        else:
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def _get_state(self):
        """Return the coordinator state for this device, if any."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self._snr)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        from .pywarema.protocol import product_type_name

        model = self._device_type_str
        if self._product_type is not None:
            model = f"{product_type_name(self._product_type)} ({self._device_type_str})"
        return DeviceInfo(
            identifiers={(DOMAIN, self._snr_hex)},
            name=self._attr_name,
            manufacturer="Warema",
            model=model,
        )

    @property
    def _level(self) -> int | None:
        """Return the current level in percent, or None when unknown.

        The dimmer reports its level in the state byte a motor uses for its
        position; 0xFF (decoded as -1) means "not available".
        """
        state = self._get_state()
        if state is None or state.position < 0:
            return None
        return state.position

    @property
    def is_on(self) -> bool | None:
        """Return True when the light is on, None while the level is unknown."""
        level = self._level
        if level is None:
            return None
        return level > 0

    @property
    def brightness(self) -> int | None:
        """Return the current brightness (0-255), or None when unknown.

        A switching actuator has no brightness; HA expects None for ONOFF.
        """
        if self._switch_only:
            return None
        level = self._level
        if level is None:
            return None
        return _wms_level_to_ha_brightness(level)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on, optionally at a given brightness."""
        if self._switch_only:
            # Only two levels exist; anything but 0 is "on".
            level = DEFAULT_ON_LEVEL
        elif ATTR_BRIGHTNESS in kwargs:
            level = _ha_brightness_to_wms_level(kwargs[ATTR_BRIGHTNESS])
            # Brightness 0 would be an "off" command; keep the light on at its
            # lowest usable level instead, matching what HA expects here.
            level = max(1, level)
        else:
            level = self._last_level or DEFAULT_ON_LEVEL

        _LOGGER.debug(
            "WaremaLight: turn_on SNR=%d (%s) level=%d%%",
            self._snr,
            self._snr_hex,
            level,
        )
        await self.hass.async_add_executor_job(
            self.coordinator.set_light_level, self._snr, level
        )
        self._last_level = level
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        # Remember the current brightness so a later "turn on" without an
        # explicit brightness restores it.
        current = self._level
        if current:
            self._last_level = current

        _LOGGER.debug("WaremaLight: turn_off SNR=%d (%s)", self._snr, self._snr_hex)
        await self.hass.async_add_executor_job(
            self.coordinator.set_light_level, self._snr, 0
        )
        self.async_write_ha_state()
