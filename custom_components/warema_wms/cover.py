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
    COVER_DEVICE_TYPES,
    CONF_DEVICES,
    DOMAIN,
    OPT_INVERT_POSITION,
    PRODUCT_TYPE_TO_DEVICE_CLASS,
    TILT_DEVICE_TYPES,
)
from .coordinator import WaremaCoordinator
from .pywarema.protocol import product_type_name

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Warema WMS cover entities from a config entry."""
    coordinator: WaremaCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    # Metadata of every cover we create, keyed by integer SNR. A valance is not
    # a device of its own - it belongs to the cover's motor - so its entity
    # reuses this to attach to the same HA device.
    cover_meta: dict[int, dict] = {}

    # Per-device position inversion, keyed by string SNR (see OPT_INVERT_POSITION).
    invert_map = entry.options.get(OPT_INVERT_POSITION, {})

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

            if device_type in COVER_DEVICE_TYPES:
                entities.append(
                    WaremaCover(
                        coordinator=coordinator,
                        snr=snr_int,
                        snr_hex=snr_hex,
                        name=name,
                        device_type=device_type,
                        device_type_str=device_type_str,
                        entry_id=entry.entry_id,
                        product_type=device.get("product_type"),
                        is_with_blinds=device.get("is_with_blinds"),
                        invert=bool(invert_map.get(str(snr_int), False)),
                    )
                )
                cover_meta[snr_int] = {
                    "snr_hex": snr_hex,
                    "name": name,
                    "device_type_str": device_type_str,
                    "product_type": device.get("product_type"),
                }
    else:
        # No devices configured: scan and add all blinds
        _LOGGER.info("No devices configured, scanning for WMS devices...")
        discovered = await coordinator.async_scan_devices(auto_assign=True)

        for device in discovered:
            device_type = device.get("device_type", "")
            if device_type in COVER_DEVICE_TYPES:
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
                        product_type=device.get("product_type"),
                        is_with_blinds=device.get("is_with_blinds"),
                        invert=bool(invert_map.get(str(snr), False)),
                    )
                )
                cover_meta[snr] = {
                    "snr_hex": snr_hex,
                    "name": name,
                    "device_type_str": device_type_str,
                    "product_type": device.get("product_type"),
                }

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Warema WMS cover entities", len(entities))
    else:
        _LOGGER.warning("No Warema WMS cover entities found")

    # ------------------------------------------------------------------
    # Valance entities
    # ------------------------------------------------------------------
    # Whether a motor drives a valance cannot be configured or asked for: the
    # position frame simply reports 0xFF for a channel that is not there, which
    # the coordinator surfaces as None. So we watch the live data and create the
    # entity the first time a channel reports a real value.
    #
    # Deliberately driven by observed data rather than by device type or by a
    # flag stored at config time: a motor that was asleep during setup reports
    # nothing, and a control entity that appears for hardware that has no
    # valance would let a user command an axis that does not exist.
    known_valances: set[tuple[int, int]] = set()

    @callback
    def _discover_valances() -> None:
        """Add valance entities for channels that have reported a value."""
        new_entities = []
        for snr, state in (coordinator.data or {}).items():
            meta = cover_meta.get(snr)
            if meta is None:
                continue
            for valance_num, value in ((1, state.valance_1), (2, state.valance_2)):
                if value is None or (snr, valance_num) in known_valances:
                    continue
                known_valances.add((snr, valance_num))
                _LOGGER.info(
                    "Valance %d detected on SNR=%d (%s), adding control entity",
                    valance_num,
                    snr,
                    meta["snr_hex"],
                )
                new_entities.append(
                    WaremaValanceCover(
                        coordinator=coordinator,
                        snr=snr,
                        snr_hex=meta["snr_hex"],
                        device_name=meta["name"],
                        device_type_str=meta["device_type_str"],
                        product_type=meta["product_type"],
                        valance_num=valance_num,
                        entry_id=entry.entry_id,
                    )
                )
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_discover_valances))
    _discover_valances()


def _device_class_for(product_type: int | None) -> CoverDeviceClass:
    """Map a productType int to the HA CoverDeviceClass.

    Defaults to BLIND when the productType is unknown - that matches the
    historical behaviour from before per-device product detection existed.
    """
    if product_type is None:
        return CoverDeviceClass.BLIND
    name = PRODUCT_TYPE_TO_DEVICE_CLASS.get(product_type)
    if name is None:
        return CoverDeviceClass.BLIND
    try:
        return CoverDeviceClass(name)
    except ValueError:
        return CoverDeviceClass.BLIND


def _supports_tilt_for(
    product_type: int | None,
    is_with_blinds: bool | None,
    device_type: str,
) -> bool:
    """Decide whether to expose tilt controls.

    Priority: the motor's own is_with_blinds flag wins. If we never managed
    to read it, fall back to the actuator-hardware whitelist that worked
    before product detection existed (in-wall actuators 20/2E).
    """
    if is_with_blinds is not None:
        return bool(is_with_blinds)
    return device_type in TILT_DEVICE_TYPES


def _wms_pos_to_ha(wms_pos: int, invert: bool = False) -> int:
    """Convert WMS position (0=open, 100=closed) to HA (0=closed, 100=open).

    When ``invert`` is set the direction is mirrored (WMS 0 -> HA 0), so a
    retracted awning (WMS 0) reads as "closed" instead of "open". The transform
    is its own inverse, so this same flag is used in both directions.
    """
    wms = max(0, min(100, wms_pos))
    return wms if invert else 100 - wms


def _ha_pos_to_wms(ha_pos: int, invert: bool = False) -> int:
    """Convert HA position (0=closed, 100=open) to WMS (0=open, 100=closed).

    See ``_wms_pos_to_ha`` for the meaning of ``invert``.
    """
    ha = max(0, min(100, ha_pos))
    return ha if invert else 100 - ha


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

    def __init__(
        self,
        coordinator: WaremaCoordinator,
        snr: int,
        snr_hex: str,
        name: str,
        device_type: str,
        device_type_str: str,
        entry_id: str,
        product_type: int | None = None,
        is_with_blinds: bool | None = None,
        invert: bool = False,
    ) -> None:
        """Initialize the cover entity."""
        super().__init__(coordinator, context=snr)
        self._snr = snr
        self._snr_hex = snr_hex
        self._device_type = device_type
        self._device_type_str = device_type_str
        self._entry_id = entry_id
        self._product_type = product_type
        self._is_with_blinds = is_with_blinds
        # Per-device HA-side position inversion (e.g. awnings); see
        # OPT_INVERT_POSITION. Mirrors displayed position + open/close commands.
        self._invert = invert

        # Device class: prefer the per-device productType if known, otherwise
        # default to BLIND (matches the pre-product-type behaviour for the
        # in-wall actuators that have historically been the only supported
        # devices).
        self._attr_device_class = _device_class_for(product_type)

        # Tilt (slat angle) requires slatted hardware. Authoritative source is
        # the motor's is_with_blinds flag (Block 37 addr 14); when that hasn't
        # been read yet, fall back to the device-type whitelist that worked
        # before product detection existed.
        self._supports_tilt = _supports_tilt_for(
            product_type, is_with_blinds, device_type
        )
        features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
            | CoverEntityFeature.SET_POSITION
        )
        if self._supports_tilt:
            features |= (
                CoverEntityFeature.OPEN_TILT
                | CoverEntityFeature.CLOSE_TILT
                | CoverEntityFeature.SET_TILT_POSITION
            )
        self._attr_supported_features = features

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
        # Prefer the product-type name (e.g. "PergolaAwning") over the generic
        # actuator-hardware string ("Plug receiver") when available - that's
        # the model users actually recognise on their facade.
        model = self._device_type_str
        if self._product_type is not None:
            model = f"{product_type_name(self._product_type)} ({self._device_type_str})"
        return DeviceInfo(
            identifiers={(DOMAIN, self._snr_hex)},
            name=self._attr_name,
            manufacturer="Warema",
            model=model,
            via_device=(DOMAIN, self._entry_id),
        )

    @property
    def current_cover_position(self) -> int | None:
        """Return current position in HA convention (0=closed, 100=open)."""
        state = self._get_blind_state()
        if not state or state.position < 0:
            return None
        return _wms_pos_to_ha(state.position, self._invert)

    @property
    def current_cover_tilt_position(self) -> int | None:
        """Return current tilt position in HA convention (0-100)."""
        if not self._supports_tilt:
            return None
        state = self._get_blind_state()
        if not state or state.position < 0:
            return None
        return _wms_angle_to_ha_tilt(state.angle)

    @property
    def is_closed(self) -> bool | None:
        """Return True if cover is fully closed.

        "Closed" is HA position 0. Normally that maps to WMS 100; when this
        device is inverted it maps to WMS 0 (e.g. a retracted awning).
        """
        state = self._get_blind_state()
        if not state or state.position < 0:
            return None
        if self._invert:
            return state.position <= 0
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
        """Open the cover (HA "open").

        Normally that is WMS position=0; for an inverted device (e.g. awning)
        HA "open" = extended = WMS position=100, so route to the opposite
        coordinator command.
        """
        _LOGGER.debug(
            "WaremaCover: open_cover SNR=%d (%s) invert=%s",
            self._snr,
            self._snr_hex,
            self._invert,
        )
        cmd = (
            self.coordinator.close_cover
            if self._invert
            else self.coordinator.open_cover
        )
        await self.hass.async_add_executor_job(cmd, self._snr)
        # Track that we initiated opening (coordinator will update actual state)
        self._command_moving = True
        self._command_is_opening = True
        self._command_is_closing = False
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover (HA "closed").

        Normally WMS position=100; for an inverted device HA "closed" =
        retracted = WMS position=0, so route to the opposite command.
        """
        _LOGGER.debug(
            "WaremaCover: close_cover SNR=%d (%s) invert=%s",
            self._snr,
            self._snr_hex,
            self._invert,
        )
        cmd = (
            self.coordinator.open_cover
            if self._invert
            else self.coordinator.close_cover
        )
        await self.hass.async_add_executor_job(cmd, self._snr)
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
        wms_pos = _ha_pos_to_wms(ha_pos, self._invert)

        state = self._get_blind_state()
        # Direction is tracked in HA terms (higher HA position = more open), so
        # it stays correct regardless of inversion.
        current_ha = self.current_cover_position

        _LOGGER.debug(
            "WaremaCover: set_cover_position SNR=%d (%s) ha=%d wms=%d invert=%s",
            self._snr,
            self._snr_hex,
            ha_pos,
            wms_pos,
            self._invert,
        )

        # Get current angle (or use 0 if unknown)
        current_angle = state.angle if state else 0

        await self.hass.async_add_executor_job(
            self.coordinator.set_position, self._snr, wms_pos, current_angle
        )
        self._command_moving = True
        if current_ha is not None:
            self._command_is_opening = ha_pos > current_ha
            self._command_is_closing = ha_pos < current_ha
        else:
            self._command_is_opening = False
            self._command_is_closing = False
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
            "product_type": self._product_type,
            "product_type_str": (
                None
                if self._product_type is None
                else product_type_name(self._product_type)
            ),
            "is_with_blinds": self._is_with_blinds,
            "wms_position": state.position if state else -1,
            "wms_angle": state.angle if state else 0,
            "moving": state.moving if state else False,
        }


class WaremaValanceCover(CoordinatorEntity[WaremaCoordinator], CoverEntity):
    """A motor's valance, exposed as a cover of its own.

    A valance (Volant) is the fabric drop at the front of an awning. It is a
    second motorised axis on the *same* motor as the cover, not a device of its
    own, so this entity attaches to the cover's HA device.

    Position convention: WMS 100 = fully lowered = HA "closed", i.e. the plain
    mapping. This deliberately ignores OPT_INVERT_POSITION, which describes
    which end of the *cover's* travel a user considers closed - the valance
    only ever drops downwards, so there is nothing to invert.
    """

    _attr_has_entity_name = True
    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(
        self,
        coordinator: WaremaCoordinator,
        snr: int,
        snr_hex: str,
        device_name: str,
        device_type_str: str,
        product_type: int | None,
        valance_num: int,
        entry_id: str,
    ) -> None:
        """Initialize the valance cover entity."""
        super().__init__(coordinator, context=snr)
        self._snr = snr
        self._snr_hex = snr_hex
        self._device_name = device_name
        self._device_type_str = device_type_str
        self._product_type = product_type
        self._valance_num = valance_num
        self._entry_id = entry_id

        self._attr_name = f"Valance {valance_num}"
        self._attr_unique_id = f"{DOMAIN}_{snr_hex}_valance_{valance_num}_cover"

        self._command_is_opening = False
        self._command_is_closing = False

    def _get_blind_state(self):
        """Get the current blind state from coordinator data."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self._snr)

    def _get_valance(self) -> int | None:
        """Return this channel's WMS valance position, or None if unknown."""
        state = self._get_blind_state()
        if not state:
            return None
        return state.valance_1 if self._valance_num == 1 else state.valance_2

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info: the same device as the cover it belongs to."""
        model = self._device_type_str
        if self._product_type is not None:
            model = f"{product_type_name(self._product_type)} ({self._device_type_str})"
        return DeviceInfo(
            identifiers={(DOMAIN, self._snr_hex)},
            name=self._device_name,
            manufacturer="Warema",
            model=model,
            via_device=(DOMAIN, self._entry_id),
        )

    @property
    def available(self) -> bool:
        """Available while the channel keeps reporting a position."""
        return super().available and self._get_valance() is not None

    @property
    def current_cover_position(self) -> int | None:
        """Return valance position in HA convention (0=lowered, 100=raised)."""
        valance = self._get_valance()
        if valance is None:
            return None
        return _wms_pos_to_ha(valance)

    @property
    def is_closed(self) -> bool | None:
        """Return True when the valance is fully lowered (WMS 100)."""
        valance = self._get_valance()
        if valance is None:
            return None
        return valance >= 100

    @property
    def is_opening(self) -> bool:
        """Return True while a raise command is in progress."""
        state = self._get_blind_state()
        return bool(state and state.moving and self._command_is_opening)

    @property
    def is_closing(self) -> bool:
        """Return True while a lower command is in progress."""
        state = self._get_blind_state()
        return bool(state and state.moving and self._command_is_closing)

    async def _async_send_valance(self, wms_valance: int) -> bool:
        """Send a valance target together with a cover position.

        The frame is one target state for every axis, so it has to name a
        cover position. We send the position the cover is already at, which
        leaves it where it is and moves only the valance.

        No cover position is refused. Verified on an awning (type 25): the
        motor never drags a lowered valance while the cover travels - it
        raises the valance, moves, and lowers it again at the destination -
        and it accepts a lowered valance at any cover position, including
        fully retracted. Both are the manufacturer's behaviour, not something
        this integration should second-guess: lowering the valance at a
        partial extension is a real use case when the sun is low.

        Returns False when the cover position is not known yet: the frame has
        to state one, and inventing a value would move the cover. Mirrors how
        the tilt commands handle the same situation.
        """
        state = self._get_blind_state()
        if not state or state.position < 0:
            _LOGGER.warning(
                "WaremaValanceCover: SNR=%d: cover position unknown, requesting update",
                self._snr,
            )
            await self.hass.async_add_executor_job(
                self.coordinator.get_position, self._snr
            )
            return False
        position = state.position

        _LOGGER.debug(
            "WaremaValanceCover: SNR=%d (%s) valance_%d -> %d (cover pos=%d)",
            self._snr,
            self._snr_hex,
            self._valance_num,
            wms_valance,
            position,
        )
        kwargs = {f"valance_{self._valance_num}": wms_valance}
        await self.hass.async_add_executor_job(
            lambda: self.coordinator.set_valance(self._snr, position, **kwargs)
        )
        return True

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Raise the valance fully (WMS 0)."""
        if not await self._async_send_valance(0):
            return
        self._command_is_opening = True
        self._command_is_closing = False
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Lower the valance fully (WMS 100)."""
        if not await self._async_send_valance(100):
            return
        self._command_is_opening = False
        self._command_is_closing = True
        self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the valance to a specific position."""
        ha_pos = kwargs[ATTR_POSITION]
        current_ha = self.current_cover_position
        if not await self._async_send_valance(_ha_pos_to_wms(ha_pos)):
            return
        if current_ha is not None:
            self._command_is_opening = ha_pos > current_ha
            self._command_is_closing = ha_pos < current_ha
        else:
            self._command_is_opening = False
            self._command_is_closing = False
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the motor.

        The stop frame addresses the motor, not one axis, so this also stops
        the cover if it happens to be moving.
        """
        _LOGGER.debug("WaremaValanceCover: stop SNR=%d (%s)", self._snr, self._snr_hex)
        await self.hass.async_add_executor_job(self.coordinator.stop, self._snr)
        self._command_is_opening = False
        self._command_is_closing = False
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Clear direction flags once the motor reports it has stopped."""
        state = self._get_blind_state()
        if state and not state.moving:
            self._command_is_opening = False
            self._command_is_closing = False
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        return {
            "snr": self._snr,
            "snr_hex": self._snr_hex,
            "valance_channel": self._valance_num,
            "wms_valance": self._get_valance(),
        }
