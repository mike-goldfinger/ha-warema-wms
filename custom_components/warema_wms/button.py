"""Button platform for Warema WMS integration.

Exposes a per-blind "Identify" button that sends a wave (beckon) request,
making the blind briefly move so the user can tell which physical device a
given entity controls.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity, ButtonDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import COVER_DEVICE_TYPES, CONF_DEVICES, DOMAIN
from .coordinator import WaremaCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Warema WMS button entities from a config entry."""
    coordinator: WaremaCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[WaremaIdentifyButton] = []

    for device in entry.data.get(CONF_DEVICES, []):
        device_type = device.get("device_type", "20")
        if device_type not in COVER_DEVICE_TYPES:
            continue

        snr = device.get("snr")
        if snr is None:
            continue
        snr_int = int(snr) if not isinstance(snr, int) else snr

        entities.append(
            WaremaIdentifyButton(
                coordinator=coordinator,
                snr=snr_int,
                snr_hex=device.get("snr_hex", ""),
                device_type_str=device.get("device_type_str", "Blind"),
                entry_id=entry.entry_id,
            )
        )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Warema WMS button entities", len(entities))


class WaremaIdentifyButton(ButtonEntity):
    """Button that sends a wave (identify) request to a blind."""

    _attr_has_entity_name = True
    _attr_device_class = ButtonDeviceClass.IDENTIFY

    def __init__(
        self,
        coordinator: WaremaCoordinator,
        snr: int,
        snr_hex: str,
        device_type_str: str,
        entry_id: str,
    ) -> None:
        self._coordinator = coordinator
        self._snr = snr
        self._snr_hex = snr_hex
        self._device_type_str = device_type_str
        self._entry_id = entry_id

        self._attr_translation_key = "identify"
        self._attr_unique_id = f"{DOMAIN}_{snr_hex}_identify"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._snr_hex)},
            name=f"{self._device_type_str} {self._snr}",
            manufacturer="Warema",
            model=self._device_type_str,
        )

    async def async_press(self) -> None:
        """Send the wave/identify request."""
        _LOGGER.debug(
            "WaremaIdentifyButton: wave SNR=%d (%s)", self._snr, self._snr_hex
        )
        await self.hass.async_add_executor_job(self._coordinator.wave, self._snr)
