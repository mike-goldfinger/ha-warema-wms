"""
Warema WMS custom integration for Home Assistant.

Provides cover entities for Warema WMS venetian blinds/stores
controlled via a WMS USB Stick (FTDI FT232R).

Serial port: /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AV0K28M2-if00-port0
Baud rate: 125000
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN
from .coordinator import WaremaCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.COVER, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Warema WMS from a config entry."""
    coordinator = WaremaCoordinator(hass, entry)

    try:
        await coordinator.async_connect()
    except Exception as exc:
        _LOGGER.error("Failed to connect to Warema WMS stick: %s", exc)
        raise ConfigEntryNotReady(f"Cannot connect to WMS stick: {exc}") from exc

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: WaremaCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_disconnect()

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
