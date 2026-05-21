"""
Cover platform for Warema WMS integration.

Each WMS blind/store is represented as a Cover entity supporting:
  - open_cover: Move to position 0 (fully open)
  - close_cover: Move to position 100 (fully closed)
  - stop_cover: Stop movement
  - set_cover_position: Move to specific position (0-100)
  - set_cover_tilt_position: Set slat angle (-100 to +100 mapped to 0-100)

Position convention:
  - HA: 0 = closed, 100 = open
  - WMS: 0 = open, 100 = closed
  → Inversion is applied in this module.

Tilt convention:
  - HA: 0 = closed/down, 100 = open/up
  - WMS: -100 = fully inward, 0 = horizontal, +100 = fully outward
  → Mapping: HA_tilt = (WMS_angle + 100) / 2
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    BLIND_DEVICE_TYPES,
    CONF_DEVICES,
    DOMAIN,
)
from .coordinator import WaremaCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Warema WMS cover entities from a config entry."""
    coordinator: WaremaCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []

    # Get devices from config entry data
    devices = entry.data.get(CONF_DEVICES, [])

    if devices:
        # Use devices from config entry.
        # NOTE: blind_add() is already called by the coordinator during
        # async_connect(), so we do NOT call it again here.
        for device in devices:
            snr = device.get("snr")
            snr_hex = device.get("snr_hex", "")
            device_type = device.get("device_type", "20")
            device_type_str = device.get("device_type_str", "Blind")
            # Ensure snr is an integer for consistent lookups
            snr_int = int(snr) if not isinstance(snr, int) else snr
            name = f"{device_type_str} {snr_int}"

            if device_type in BLIND_DEVICE_TYPES:
                entities.append(
                    WaremaCover(
                        coordinator=coordinator,
                        snr=snr_int,
                        snr_hex=snr_hex,
                        name=name,
                        device_type=device_type,
                        device_type_str=device_type_str,
                        entry_id=entry.entry_id,
                    )
                )
    else:
        # No devices configured: scan and add all blinds
        _LOGGER.info("No devices configured, scanning for WMS devices...")
        discovered = await coordinator.async_scan_devices(auto_assign=True)

        for device in discovered:
            device_type = device.get("device_type", "")
            if device_type in BLIND_DEVICE_TYPES:
                snr = device.get("snr")
                snr_hex = device.get("snr_hex", "")
                device_type_str = device.get("device_type_str", "Blind")
                name = f"{device_type_str} {snr}"
                entities.append(
                    WaremaCover(
                        coordinator=coordinator,
                        snr=snr,
                        snr_hex=snr_hex,
                        name=name,
                        device_type=device_type,
                        device_type_str=device_type_str,
                        entry_id=entry.entry_id,
                    )
                )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Warema WMS cover entities", len(entities))
    else:
        _LOGGER.warning("No Warema WMS cover entities found")


def _wms_pos_to_ha(wms_pos: int) -> int:
    """Convert WMS position (0=open, 100=closed) to HA (0=closed, 100=open)."""
    return 100 - max(0, min(100, wms_pos))


def _ha_pos_to_wms(ha_pos: int) -> int:
    """Convert HA position (0=closed, 100=open) to WMS (0=open, 100=closed)."""
    return 100 - max(0, min(100, ha_pos))


def _wms_angle_to_ha_tilt(wms_angle: int) -> int:
    """Convert WMS angle (-100 to +100) to HA tilt (0-100).

    WMS -100 (fully inward/closed) → HA 100
    WMS 0 (horizontal) → HA 50
    WMS +100 (fully outward/open) → HA 0

    Inverted to match HA convention: 0=closed, 100=open.
    """
    clamped = max(-100, min(100, wms_angle))
    return round((100 - clamped) / 2)


def _ha_tilt_to_wms_angle(ha_tilt: int) -> int:
    """Convert HA tilt (0-100) to WMS angle (-100 to +100).

    HA 0 (closed) → WMS +100
    HA 50 (horizontal) → WMS 0
    HA 100 (open) → WMS -100

    Inverted to match HA convention: 0=closed, 100=open.
    """
    clamped = max(0, min(100, ha_tilt))
    return round((100 - clamped) * 2 - 100)


class WaremaCover(CoordinatorEntity[WaremaCoordinator], CoverEntity):
    """Representation of a Warema WMS blind as a HA Cover entity."""

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.BLIND
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
        | CoverEntityFeature.OPEN_TILT
        | CoverEntityFeature.CLOSE_TILT
        | CoverEntityFeature.SET_TILT_POSITION
    )

    def __init__(
        self,
        coordinator: WaremaCoordinator,
        snr: int,
        snr_hex: str,
        name: str,
        device_type: str,
        device_type_str: str,
        entry_id: str,
    ) -> None:
        """Initialize the cover entity."""
        super().__init__(coordinator, context=snr)
        self._snr = snr
        self._snr_hex = snr_hex
        self._device_type = device_type
        self._device_type_str = device_type_str
        self._entry_id = entry_id

        # For tracking movement direction during commands (not persisted across updates)
        self._command_moving = False
        self._command_is_opening = False
        self._command_is_closing = False

        # HA entity attributes
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{snr_hex}"

    def _get_blind_state(self):
        """Get the current blind state from coordinator data."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self._snr)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for this cover."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._snr_hex)},
            name=self._attr_name,
            manufacturer="Warema",
            model=self._device_type_str,
            via_device=(DOMAIN, self._entry_id),
        )

    @property
    def current_cover_position(self) -> int | None:
        """Return current position in HA convention (0=closed, 100=open)."""
        state = self._get_blind_state()
        if not state or state.position < 0:
            return None
        return _wms_pos_to_ha(state.position)

    @property
    def current_cover_tilt_position(self) -> int | None:
        """Return current tilt position in HA convention (0-100)."""
        state = self._get_blind_state()
        if not state or state.position < 0:
            return None
        return _wms_angle_to_ha_tilt(state.angle)

    @property
    def is_closed(self) -> bool | None:
        """Return True if cover is fully closed."""
        state = self._get_blind_state()
        if not state or state.position < 0:
            return None
        return state.position >= 100

    @property
    def is_opening(self) -> bool:
        """Return True if cover is currently opening (moving toward WMS 0 = open)."""
        state = self._get_blind_state()
        return bool(state and state.moving and self._command_is_opening)

    @property
    def is_closing(self) -> bool:
        """Return True if cover is currently closing (moving toward WMS 100 = closed)."""
        state = self._get_blind_state()
        return bool(state and state.moving and self._command_is_closing)

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover (WMS position=0, angle=-100 = fully open)."""
        _LOGGER.debug("WaremaCover: open_cover SNR=%d (%s)", self._snr, self._snr_hex)
        await self.hass.async_add_executor_job(self.coordinator.open_cover, self._snr)
        # Track that we initiated opening (coordinator will update actual state)
        self._command_moving = True
        self._command_is_opening = True
        self._command_is_closing = False
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover (WMS position=100, angle=100 = fully closed)."""
        _LOGGER.debug("WaremaCover: close_cover SNR=%d (%s)", self._snr, self._snr_hex)
        await self.hass.async_add_executor_job(self.coordinator.close_cover, self._snr)
        # Track that we initiated closing (coordinator will update actual state)
        self._command_moving = True
        self._command_is_opening = False
        self._command_is_closing = True
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        _LOGGER.debug("WaremaCover: stop_cover SNR=%d (%s)", self._snr, self._snr_hex)
        await self.hass.async_add_executor_job(self.coordinator.stop, self._snr)
        # Coordinator will update actual state via callback
        self._command_moving = False
        self._command_is_opening = False
        self._command_is_closing = False
        self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the cover to a specific position.

        HA position: 0=closed, 100=open
        WMS position: 0=open, 100=closed  (inverted)
        """
        ha_pos = kwargs[ATTR_POSITION]
        wms_pos = _ha_pos_to_wms(ha_pos)

        state = self._get_blind_state()
        current_wms = max(0, state.position if state else -1)

        _LOGGER.debug(
            "WaremaCover: set_cover_position SNR=%d (%s) ha=%d wms=%d",
            self._snr,
            self._snr_hex,
            ha_pos,
            wms_pos,
        )

        # Get current angle (or use 0 if unknown)
        current_angle = state.angle if state else 0

        await self.hass.async_add_executor_job(
            self.coordinator.set_position, self._snr, wms_pos, current_angle
        )
        self._command_moving = True
        # Opening = moving toward WMS 0 (lower WMS value = more open)
        self._command_is_opening = wms_pos < current_wms
        self._command_is_closing = wms_pos > current_wms
        self.async_write_ha_state()

    async def async_open_cover_tilt(self, **kwargs: Any) -> None:
        """Open the tilt (slats to WMS angle -100), position unchanged.

        Mirrors the manufacturer's vnBlindSetPosition: the angle is sent via a
        blindMoveToPos command carrying the *current* position, so only the
        slats turn while the blind stays where it is.
        """
        state = self._get_blind_state()
        if not state or state.position < 0:
            _LOGGER.warning(
                "WaremaCover: open_cover_tilt SNR=%d: position unknown, requesting update",
                self._snr,
            )
            await self.hass.async_add_executor_job(
                self.coordinator.get_position, self._snr
            )
            return

        _LOGGER.debug(
            "WaremaCover: open_cover_tilt SNR=%d (%s) pos=%d angle=-100",
            self._snr,
            self._snr_hex,
            state.position,
        )
        await self.hass.async_add_executor_job(
            self.coordinator.set_position, self._snr, state.position, -100
        )
        self._command_moving = True
        self.async_write_ha_state()

    async def async_close_cover_tilt(self, **kwargs: Any) -> None:
        """Close the tilt (slats to WMS angle +100), position unchanged."""
        state = self._get_blind_state()
        if not state or state.position < 0:
            _LOGGER.warning(
                "WaremaCover: close_cover_tilt SNR=%d: position unknown, requesting update",
                self._snr,
            )
            await self.hass.async_add_executor_job(
                self.coordinator.get_position, self._snr
            )
            return

        _LOGGER.debug(
            "WaremaCover: close_cover_tilt SNR=%d (%s) pos=%d angle=+100",
            self._snr,
            self._snr_hex,
            state.position,
        )
        await self.hass.async_add_executor_job(
            self.coordinator.set_position, self._snr, state.position, 100
        )
        self._command_moving = True
        self.async_write_ha_state()

    async def async_set_cover_tilt_position(self, **kwargs: Any) -> None:
        """Set the slat angle at the current position (no position change).

        HA tilt: 0-100 → WMS angle: -100 to +100. Sent via blindMoveToPos with
        the current position unchanged, matching the manufacturer's
        vnBlindSetPosition / vnBlindSlatTiltOver behaviour.
        """
        ha_tilt = kwargs[ATTR_TILT_POSITION]
        wms_angle = _ha_tilt_to_wms_angle(ha_tilt)
        state = self._get_blind_state()
        if not state or state.position < 0:
            _LOGGER.warning(
                "WaremaCover: set_cover_tilt_position SNR=%d: position unknown, requesting update",
                self._snr,
            )
            await self.hass.async_add_executor_job(
                self.coordinator.get_position, self._snr
            )
            return

        _LOGGER.debug(
            "WaremaCover: set_cover_tilt_position SNR=%d (%s) ha_tilt=%d wms_angle=%d pos=%d",
            self._snr,
            self._snr_hex,
            ha_tilt,
            wms_angle,
            state.position,
        )
        await self.hass.async_add_executor_job(
            self.coordinator.set_position, self._snr, state.position, wms_angle
        )
        self._command_moving = True
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.

        Clears command flags when the actual state is updated.
        """
        state = self._get_blind_state()
        if state and not state.moving:
            # Clear direction flags when movement stops
            self._command_moving = False
            self._command_is_opening = False
            self._command_is_closing = False
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        state = self._get_blind_state()
        return {
            "snr": self._snr,
            "snr_hex": self._snr_hex,
            "device_type": self._device_type,
            "device_type_str": self._device_type_str,
            "wms_position": state.position if state else -1,
            "wms_angle": state.angle if state else 0,
            "moving": state.moving if state else False,
        }
