"""
Config flow for Warema WMS integration.

Provides UI-based setup via Home Assistant's config flow system.

Wizard steps:
  1. serial_port  – User selects the serial port (or it is pre-filled from USB discovery)
  2. discovery    – User chooses HOW to obtain the network parameters:
                      a) manual       – enter Channel / PAN ID / Network Key by hand
                      b) wandsender   – put Wandsender into pairing mode; the wizard
                                        listens for a joinNetworkRequest and captures
                                        the parameters automatically
                      c) new_network  – generate a random PAN ID + key, put motors
                                        into pairing mode, and let them join
  3. manual       – (only for mode "manual") enter Channel, PAN ID, Network Key
  4. wandsender   – (only for mode "wandsender") waiting screen; parameters are
                    captured automatically from the Wandsender pairing procedure
  5. new_network  – (only for mode "new_network") waiting screen; parameters are
                    captured when the first motor joins
  6. devices      – multi-select which discovered blinds to add
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import threading
from typing import Any

import serial
import voluptuous as vol
from serial.tools import list_ports

from homeassistant import config_entries
from homeassistant.components import usb
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult, section
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    COVER_DEVICE_TYPES,
    SUPPORTED_DEVICE_TYPES,
    CONF_CHANNEL,
    CONF_DEVICES,
    CONF_DISCOVERY_MODE,
    CONF_NETWORK_KEY,
    CONF_PAN_ID,
    CONF_SERIAL_PORT,
    DEFAULT_CHANNEL,
    DEFAULT_NEW_NETWORK_CHANNEL,
    DEFAULT_SERIAL_PORT,
    DISCOVERY_MODE_MANUAL,
    DISCOVERY_MODE_NEW_NETWORK,
    DISCOVERY_MODE_WANDSENDER,
    DOMAIN,
    OPT_INVERT_POSITION,
)

_LOGGER = logging.getLogger(__name__)

# Validation patterns used in async_step_manual for manual field validation.
PAN_ID_PATTERN = re.compile(r"^[0-9A-Fa-f]{4}$")
KEY_PATTERN = re.compile(r"^[0-9A-Fa-f]{32}$")

# Probe PAN ID used when listening for Wandsender pairing
_PAN_ID_PROBE = "FFFF"
_KEY_DUMMY = "00112233445566778899AABBCCDDEEFF"

# How long to wait for Wandsender / motor pairing (seconds)
_PAIRING_TIMEOUT = 300  # 5 minutes

# FTDI FT232R USB UART chip VID/PID
_FTDI_VID = 0x0403
_FTDI_PID = 0x6001


# ---------------------------------------------------------------------------
# Serial port discovery helpers
# ---------------------------------------------------------------------------


def _find_ftdi_devices() -> list[tuple[str, str]]:
    """Find available FTDI FT232R USB UART devices.

    Returns:
        List of (port, description) tuples for FTDI devices found.
    """
    found_devices = []
    for port_info in list_ports.comports():
        # Check for FTDI VID/PID
        if port_info.vid == _FTDI_VID and port_info.pid == _FTDI_PID:
            desc = port_info.description or port_info.device
            found_devices.append((port_info.device, desc))
    return found_devices


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

STEP_SERIAL_PORT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SERIAL_PORT, default=DEFAULT_SERIAL_PORT): str,
    }
)

STEP_DISCOVERY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DISCOVERY_MODE, default=DISCOVERY_MODE_MANUAL): vol.In(
            [
                DISCOVERY_MODE_MANUAL,
                DISCOVERY_MODE_WANDSENDER,
                DISCOVERY_MODE_NEW_NETWORK,
            ]
        ),
    }
)

STEP_MANUAL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CHANNEL, default=DEFAULT_CHANNEL): int,
        vol.Required(CONF_PAN_ID): str,
        vol.Required(CONF_NETWORK_KEY): str,
    }
)


# ---------------------------------------------------------------------------
# Low-level serial helpers (run in executor)
# ---------------------------------------------------------------------------


def _init_stick_probe(port: str, channel: int, pan_id: str, key: str) -> serial.Serial:
    """Open the serial port and send the WMS initialization sequence.

    Returns the open serial.Serial object.
    Raises serial.SerialException on failure.
    """
    ser = serial.Serial(
        port=port,
        baudrate=125000,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.1,
    )
    import time

    time.sleep(0.2)
    ser.write(b"{G}")
    time.sleep(0.1)
    ser.write(b"{V}")
    time.sleep(0.1)
    ser.write(("{K401" + key + "}").encode("ascii"))
    time.sleep(0.3)
    ser.write(("{M%" + str(channel) + pan_id + "}").encode("ascii"))
    time.sleep(0.5)
    return ser


def _listen_for_network_params(
    ser: serial.Serial,
    pan_id_probe: str,
    stop_event: threading.Event,
    result_holder: dict,
    stick_found_event: threading.Event,
    params_captured_event: threading.Event,
) -> None:
    """Background thread: listen for joinNetworkRequest and capture network params.

    Handles scanRequest / waveRequest / switchChannelRequest so the Wandsender
    can find and pair with the stick.

    Real-time pairing state is exposed via ``result_holder`` and two
    ``threading.Event`` objects so the config flow can drive an
    ``async_show_progress`` dialog:

      * ``result_holder['phase']``  – "scanning" | "stick_found" | "params_captured"
      * ``result_holder['snr']``    – the SNR of the Handsender that scanned us
      * ``stick_found_event``       – set when the stick is identified (waveRequest)
      * ``params_captured_event``   – set when params arrive (joinNetworkRequest)

    Sets result_holder['params'] when parameters are captured.
    Sets result_holder['error'] on serial error.
    """
    from .pywarema.protocol import decode_frame

    recv_buffer = ""

    while not stop_event.is_set():
        try:
            data = ser.read(256)
        except serial.SerialException as exc:
            _LOGGER.warning("WANDSENDER listener serial error: %s", exc)
            result_holder["error"] = str(exc)
            # Unblock any waiters so the flow can surface the error.
            stick_found_event.set()
            params_captured_event.set()
            return

        if data:
            recv_buffer += data.decode("ascii", errors="replace")

        while "}" in recv_buffer:
            idx = recv_buffer.index("}")
            frame = recv_buffer[: idx + 1]
            recv_buffer = recv_buffer[idx + 1 :]

            if not frame.startswith("{"):
                continue

            try:
                msg = decode_frame(frame)
            except Exception:  # pylint: disable=broad-except
                continue

            msg_type = msg.get("msg_type", "")
            params = msg.get("params", {})
            snr = msg.get("snr", "000000")

            if msg_type == "joinNetworkRequest":
                result_holder["params"] = {
                    CONF_CHANNEL: params.get("channel", DEFAULT_CHANNEL),
                    CONF_PAN_ID: params.get("pan_id", "").upper(),
                    CONF_NETWORK_KEY: params.get("network_key", "").upper(),
                }
                result_holder["phase"] = "params_captured"
                # ACK back
                try:
                    ser.write(b"{a}")
                except Exception:  # pylint: disable=broad-except
                    pass
                # Unblock both waiters (stick_found may have been missed if the
                # user paired very quickly).
                stick_found_event.set()
                params_captured_event.set()
                return

            elif msg_type == "scanRequest":
                # IMPORTANT: Always answer with the probe PAN ID (FFFF), NOT the
                # network's real PAN ID. This keeps the stick "disguised" as a new
                # stick so the Handsender will send a joinNetworkRequest (which
                # contains the network key) when STOP is pressed. If we answered
                # with the real PAN ID here, the Handsender would recognize the
                # stick as already known and never send the key.
                result_holder["snr"] = snr
                result_holder.setdefault("phase", "scanning")
                try:
                    resp = ("{R01" + snr + "7021" + pan_id_probe + "02}").encode(
                        "ascii"
                    )
                    ser.write(resp)
                except Exception:  # pylint: disable=broad-except
                    pass

            elif msg_type == "waveRequest":
                # The Handsender has identified our stick ("Waving and Hello!").
                # This is the moment the user must press the STOP button.
                result_holder["snr"] = snr
                result_holder["phase"] = "stick_found"
                stick_found_event.set()
                try:
                    ser.write(b"{a}")
                except Exception:  # pylint: disable=broad-except
                    pass

            elif msg_type == "switchChannelRequest":
                # Follow the physical channel switch so we can talk to the
                # Handsender, but do NOT change current_pan_id — the scanResponse
                # above must keep answering with the probe PAN ID (FFFF).
                ch = params.get("channel", DEFAULT_CHANNEL)
                pid = params.get("pan_id", pan_id_probe)
                try:
                    ser.write(("{M%" + str(ch) + pid + "}").encode("ascii"))
                except Exception:  # pylint: disable=broad-except
                    pass


def _listen_for_paired_device(
    ser: serial.Serial,
    pan_id: str,
    stop_event: threading.Event,
    result_holder: dict,
    stick_found_event: threading.Event,
    device_found_event: threading.Event,
) -> None:
    """Background thread: capture a device that only answers via its Wandsender.

    Some actuators (observed on Warema Lamaxa slat-roof lighting, see
    https://github.com/mike-goldfinger/ha-warema-wms/issues/8) never joined the
    WMS network on their own and so never answer a plain broadcast scanRequest -
    they were only ever paired to a handheld Wandsender. Such a device can be
    revealed by talking to the Wandsender itself instead of the device
    directly; this listener reproduces that path for a single extra device
    without touching the existing network's PAN ID or key.

    Unlike ``_listen_for_network_params`` (used for initial setup), this
    listener answers scanRequest with our REAL pan_id, not a probe ID: we are
    not trying to look like an unknown stick to capture a fresh network key -
    we already have the network's key and only need the Wandsender to nudge its
    paired device into announcing itself with a joinNetworkRequest, which tells
    us that device's serial number.

    Real-time state is exposed the same way ``_listen_for_network_params`` does:

      * ``result_holder['phase']`` - "scanning" | "stick_found" | "device_found"
      * ``result_holder['snr']``   - SNR of the Wandsender that scanned us
      * ``result_holder['device_snr']`` / ``['device_snr_hex']`` - captured device
      * ``stick_found_event``      - set when the Wandsender identifies our stick
      * ``device_found_event``     - set when the paired device announces itself
    """
    from .pywarema.protocol import decode_frame

    recv_buffer = ""

    while not stop_event.is_set():
        try:
            data = ser.read(256)
        except serial.SerialException as exc:
            _LOGGER.warning("PAIR_DEVICE listener serial error: %s", exc)
            result_holder["error"] = str(exc)
            stick_found_event.set()
            device_found_event.set()
            return

        if data:
            recv_buffer += data.decode("ascii", errors="replace")

        while "}" in recv_buffer:
            idx = recv_buffer.index("}")
            frame = recv_buffer[: idx + 1]
            recv_buffer = recv_buffer[idx + 1 :]

            if not frame.startswith("{"):
                continue

            try:
                msg = decode_frame(frame)
            except Exception:  # pylint: disable=broad-except
                continue

            msg_type = msg.get("msg_type", "")
            params = msg.get("params", {})
            snr = msg.get("snr", "000000")

            if msg_type == "joinNetworkRequest":
                # The paired device (not the Wandsender) is announcing itself.
                result_holder["device_snr"] = msg.get("snr_num")
                result_holder["device_snr_hex"] = snr
                result_holder["phase"] = "device_found"
                try:
                    ser.write(b"{a}")
                except Exception:  # pylint: disable=broad-except
                    pass
                device_found_event.set()
                return

            elif msg_type == "scanRequest":
                # Answer with our real PAN ID: the Wandsender already knows this
                # stick, so it won't offer to (re)send a network key - we don't
                # want one. We only need it to keep talking to its paired device.
                result_holder["snr"] = snr
                result_holder.setdefault("phase", "scanning")
                try:
                    resp = ("{R01" + snr + "7021" + pan_id + "02}").encode("ascii")
                    ser.write(resp)
                except Exception:  # pylint: disable=broad-except
                    pass

            elif msg_type == "waveRequest":
                result_holder["snr"] = snr
                result_holder["phase"] = "stick_found"
                stick_found_event.set()
                try:
                    ser.write(b"{a}")
                except Exception:  # pylint: disable=broad-except
                    pass

            elif msg_type == "switchChannelRequest":
                ch = params.get("channel", DEFAULT_CHANNEL)
                pid = params.get("pan_id", pan_id)
                try:
                    ser.write(("{M%" + str(ch) + pid + "}").encode("ascii"))
                except Exception:  # pylint: disable=broad-except
                    pass


def _listen_for_motor_join(
    ser: serial.Serial,
    pan_id: str,
    stop_event: threading.Event,
    result_holder: dict,
) -> None:
    """Background thread: listen for a motor joinNetworkRequest.

    Used in 'new_network' mode where we already know the credentials and
    just wait for a motor to join.

    Sets result_holder['snr'] when a motor joins.
    Sets result_holder['error'] on serial error.
    """
    from .pywarema.protocol import decode_frame

    recv_buffer = ""

    while not stop_event.is_set():
        try:
            data = ser.read(256)
        except serial.SerialException as exc:
            result_holder["error"] = str(exc)
            return

        if data:
            recv_buffer += data.decode("ascii", errors="replace")

        while "}" in recv_buffer:
            idx = recv_buffer.index("}")
            frame = recv_buffer[: idx + 1]
            recv_buffer = recv_buffer[idx + 1 :]

            if not frame.startswith("{"):
                continue

            try:
                msg = decode_frame(frame)
            except Exception:  # pylint: disable=broad-except
                continue

            msg_type = msg.get("msg_type", "")
            params = msg.get("params", {})
            snr = msg.get("snr", "000000")

            if msg_type == "joinNetworkRequest":
                result_holder["snr"] = msg.get("snr_num")
                result_holder["snr_hex"] = snr
                try:
                    ser.write(b"{a}")
                except Exception:  # pylint: disable=broad-except
                    pass
                return

            elif msg_type == "scanRequest":
                try:
                    resp = ("{R01" + snr + "7021" + pan_id + "02}").encode("ascii")
                    ser.write(resp)
                except Exception:  # pylint: disable=broad-except
                    pass

            elif msg_type == "waveRequest":
                try:
                    ser.write(b"{a}")
                except Exception:  # pylint: disable=broad-except
                    pass

            elif msg_type == "switchChannelRequest":
                ch = params.get("channel", DEFAULT_NEW_NETWORK_CHANNEL)
                pid = params.get("pan_id", pan_id)
                try:
                    ser.write(("{M%" + str(ch) + pid + "}").encode("ascii"))
                except Exception:  # pylint: disable=broad-except
                    pass


# ---------------------------------------------------------------------------
# Connection test (used after manual entry)
# ---------------------------------------------------------------------------


async def _test_connection(
    hass: HomeAssistant, port: str, channel: int, pan_id: str, key: str
) -> dict[str, Any]:
    """Test connection to WMS stick and scan for devices.

    Returns:
        dict with 'devices' list on success.

    Raises:
        CannotConnect: If connection fails.
    """
    from .pywarema.stick import WmsStick

    init_event = asyncio.Event()
    scan_event = asyncio.Event()
    devices = []
    error_msg = None

    def _cb(error, msg):
        nonlocal error_msg, devices
        if error and error != "timeout":
            error_msg = error
        if not msg:
            return
        topic = msg.get("topic", "")
        if topic == "wms-vb-init-completion":
            hass.loop.call_soon_threadsafe(init_event.set)
        elif topic == "wms-vb-scanned-devices":
            devices = msg.get("payload", {}).get("devices", [])
            hass.loop.call_soon_threadsafe(scan_event.set)

    stick = WmsStick(
        port=port,
        channel=channel,
        pan_id=pan_id,
        key=key,
        callback=_cb,
    )

    try:
        await hass.async_add_executor_job(stick.connect)
        await asyncio.wait_for(init_event.wait(), timeout=15.0)

        await hass.async_add_executor_job(stick.scan_devices, False)
        try:
            await asyncio.wait_for(scan_event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            _LOGGER.warning("Device scan timed out during config flow")

        # For each discovered blind, try to read the productType from Block 37
        # so the user sees the right model name and the HA cover entity gets
        # the right device_class / tilt support out of the box. This is a
        # best-effort enrichment - if the motor is asleep the read returns
        # None and we just leave the fields unset (coordinator will retry
        # at runtime).
        # Lazy import to avoid pulling pywarema at module load time.
        from .pywarema.protocol import product_type_name

        for dev in devices:
            if dev.get("device_type", "") not in SUPPORTED_DEVICE_TYPES:
                continue
            snr = dev.get("snr")
            if snr is None:
                continue
            # blind_add is required so read_product_info can resolve the SNR.
            await hass.async_add_executor_job(
                stick.blind_add, int(snr), f"discovery-{snr}"
            )
            info = await hass.async_add_executor_job(stick.read_product_info, int(snr))
            if info is not None:
                product_type, is_with_blinds = info
                dev["product_type"] = product_type
                dev["is_with_blinds"] = is_with_blinds
                dev["product_type_str"] = product_type_name(product_type)

    except asyncio.TimeoutError as exc:
        raise CannotConnect("Connection timed out") from exc
    except Exception as exc:
        raise CannotConnect(str(exc)) from exc
    finally:
        await hass.async_add_executor_job(stick.disconnect)

    return {"devices": devices}


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------


class WaremaWmsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Warema WMS."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._user_input: dict[str, Any] = {}
        self._discovered_devices: list[dict] = []
        self._usb_discovery_info: usb.UsbServiceInfo | None = None
        # Used by wandsender / new_network steps
        self._serial_obj: serial.Serial | None = None
        self._stop_event: threading.Event | None = None
        self._listen_thread: threading.Thread | None = None
        self._result_holder: dict = {}
        # Wandsender pairing progress state
        self._stick_found_event: threading.Event | None = None
        self._params_captured_event: threading.Event | None = None
        self._wandsender_phase: str | None = None
        self._wandsender_abort_reason: str | None = None
        self._wandsender_task: asyncio.Task | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "WaremaWmsOptionsFlow":
        """Return the options flow handler.

        Note: do NOT pass the config entry into the flow. Since HA 2024.11
        ``OptionsFlow.config_entry`` is a read-only property provided by the
        base class; assigning it raises AttributeError.
        """
        return WaremaWmsOptionsFlow()

    # ------------------------------------------------------------------
    # USB auto-discovery
    # ------------------------------------------------------------------

    async def async_step_usb(self, discovery_info: usb.UsbServiceInfo) -> FlowResult:
        """Handle USB discovery of the WMS Stick (FTDI FT232R)."""
        await self.async_set_unique_id(
            f"{discovery_info.vid}:{discovery_info.pid}:{discovery_info.serial_number}"
        )
        self._abort_if_unique_id_configured()

        self._usb_discovery_info = discovery_info
        self._user_input[CONF_SERIAL_PORT] = discovery_info.device

        return await self.async_step_usb_confirm()

    async def async_step_usb_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm USB discovery and proceed to serial port step."""
        assert self._usb_discovery_info is not None
        discovery_info = self._usb_discovery_info

        if user_input is not None:
            return await self.async_step_serial_port()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="usb_confirm",
            description_placeholders={
                "device": discovery_info.device,
                "manufacturer": discovery_info.manufacturer or "FTDI",
                "description": discovery_info.description or "FT232R USB UART",
                "serial_number": discovery_info.serial_number or "",
            },
        )

    # ------------------------------------------------------------------
    # Step 1: serial_port
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Entry point for manual setup – go straight to serial port step."""
        return await self.async_step_serial_port(user_input)

    async def async_step_serial_port(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: Ask for the serial port path.

        Automatically discovers FTDI FT232R devices and offers them in a dropdown,
        or auto-selects if only one is found.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            port = user_input.get(CONF_SERIAL_PORT, "").strip()
            if not port:
                errors[CONF_SERIAL_PORT] = "invalid_serial_port"
            else:
                self._user_input[CONF_SERIAL_PORT] = port
                return await self.async_step_discovery()

        # Auto-discover FTDI devices
        ftdi_devices = await self.hass.async_add_executor_job(_find_ftdi_devices)

        # If exactly one FTDI device found, auto-select it and continue
        if len(ftdi_devices) == 1:
            self._user_input[CONF_SERIAL_PORT] = ftdi_devices[0][0]
            return await self.async_step_discovery()

        # Build schema based on devices found
        if ftdi_devices:
            # Multiple devices found: offer dropdown
            port_choices = {port: desc for port, desc in ftdi_devices}
            default_port = ftdi_devices[0][0]

            schema = vol.Schema(
                {
                    vol.Required(CONF_SERIAL_PORT, default=default_port): vol.In(
                        port_choices
                    ),
                }
            )

            devices_list = "\n".join(
                [f"- {desc} ({port})" for port, desc in ftdi_devices]
            )
        else:
            # No FTDI devices found: show manual text input
            default_port = self._user_input.get(CONF_SERIAL_PORT, DEFAULT_SERIAL_PORT)
            schema = vol.Schema(
                {
                    vol.Required(CONF_SERIAL_PORT, default=default_port): str,
                }
            )

            devices_list = "(No devices detected)"

        return self.async_show_form(
            step_id="serial_port",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "devices_list": devices_list,
                "default_port": DEFAULT_SERIAL_PORT,
            },
        )

    # ------------------------------------------------------------------
    # Step 2: discovery mode selection
    # ------------------------------------------------------------------

    async def async_step_discovery(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2: Choose how to discover the network parameters."""
        if user_input is not None:
            mode = user_input.get(CONF_DISCOVERY_MODE, DISCOVERY_MODE_MANUAL)
            self._user_input[CONF_DISCOVERY_MODE] = mode

            if mode == DISCOVERY_MODE_WANDSENDER:
                return await self.async_step_wandsender()
            elif mode == DISCOVERY_MODE_NEW_NETWORK:
                return await self.async_step_new_network()
            else:
                return await self.async_step_manual()

        return self.async_show_form(
            step_id="discovery",
            data_schema=STEP_DISCOVERY_SCHEMA,
        )

    # ------------------------------------------------------------------
    # Step 3a: manual entry
    # ------------------------------------------------------------------

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3a: Manual entry of Channel, PAN ID and Network Key."""
        errors: dict[str, str] = {}

        if user_input is not None:
            pan_id = user_input.get(CONF_PAN_ID, "")
            network_key = user_input.get(CONF_NETWORK_KEY, "")
            channel = user_input.get(CONF_CHANNEL, DEFAULT_CHANNEL)

            if not PAN_ID_PATTERN.match(pan_id):
                errors[CONF_PAN_ID] = "invalid_pan_id"
            if not KEY_PATTERN.match(network_key):
                errors[CONF_NETWORK_KEY] = "invalid_key"
            if not isinstance(channel, int) or not (1 <= channel <= 26):
                errors[CONF_CHANNEL] = "invalid_channel"

            if not errors:
                try:
                    result = await _test_connection(
                        self.hass,
                        self._user_input[CONF_SERIAL_PORT],
                        channel,
                        pan_id.upper(),
                        network_key.upper(),
                    )
                    self._user_input[CONF_CHANNEL] = channel
                    self._user_input[CONF_PAN_ID] = pan_id.upper()
                    self._user_input[CONF_NETWORK_KEY] = network_key.upper()
                    self._discovered_devices = result.get("devices", [])

                    if self._discovered_devices:
                        return await self.async_step_devices()
                    return self._create_entry()

                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("Unexpected error during WMS connection test")
                    errors["base"] = "unknown"

        return self.async_show_form(
            step_id="manual",
            data_schema=STEP_MANUAL_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 3b: Wandsender pairing (capture network params)
    # ------------------------------------------------------------------

    async def async_step_wandsender(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Wrapper around the wandsender step that logs any raised exception.

        HA's FlowManager removes a flow whose step raises, after which the
        frontend shows "Invalid flow specified". Logging the exception here
        (via the working custom_components.warema_wms logger) makes the real
        cause visible instead of the generic frontend message.
        """
        try:
            return await self._wandsender_impl(user_input)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception(
                "WANDSENDER step crashed (phase=%s)", self._wandsender_phase
            )
            raise

    async def _wandsender_impl(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3b: Listen for Wandsender pairing using a live progress dialog.

        Uses Home Assistant's ``async_show_progress`` pattern so the user sees
        an animated dialog that updates as the pairing protocol advances:

          * Phase "waiting"     – wait for the Handsender to identify the stick
                                  (waveRequest). The dialog shows the button
                                  sequence the user must perform.
          * Phase "press_stop"  – the stick was found; the dialog tells the user
                                  to press the STOP button NOW (joinNetworkRequest).
          * Phase "connecting"  – params captured; reconnect with the real network
                                  parameters and scan for blinds.

        Home Assistant automatically re-invokes this step whenever the current
        ``progress_task`` finishes, so each call advances to the next phase.
        """
        _LOGGER.debug(
            "WANDSENDER entry: phase=%s error=%s stick_found=%s params_captured=%s params=%s",
            self._wandsender_phase,
            (self._result_holder or {}).get("error"),
            self._stick_found_event.is_set() if self._stick_found_event else None,
            (
                self._params_captured_event.is_set()
                if self._params_captured_event
                else None
            ),
            bool((self._result_holder or {}).get("params")),
        )
        # --- First entry: open serial port + start the listener thread ---
        # Note: use _wandsender_phase (not _listen_thread) to detect the first
        # entry, because _cleanup_serial() resets _listen_thread to None during
        # the later phases.
        if self._wandsender_phase is None:
            port = self._user_input[CONF_SERIAL_PORT]
            try:
                self._serial_obj = await self.hass.async_add_executor_job(
                    _init_stick_probe, port, DEFAULT_CHANNEL, _PAN_ID_PROBE, _KEY_DUMMY
                )
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.error(
                    "Failed to open serial port for Wandsender pairing: %s", exc
                )
                # No progress dialog has been shown yet, so abort directly.
                return self.async_abort(reason="cannot_connect")

            self._result_holder = {}
            self._stop_event = threading.Event()
            self._stick_found_event = threading.Event()
            self._params_captured_event = threading.Event()
            self._listen_thread = threading.Thread(
                target=_listen_for_network_params,
                args=(
                    self._serial_obj,
                    _PAN_ID_PROBE,
                    self._stop_event,
                    self._result_holder,
                    self._stick_found_event,
                    self._params_captured_event,
                ),
                daemon=True,
                name="wms-wandsender-listener",
            )
            self._listen_thread.start()

            self._wandsender_phase = "waiting"
            self._wandsender_task = self.hass.async_create_task(
                asyncio.to_thread(self._stick_found_event.wait, _PAIRING_TIMEOUT)
            )
            return self.async_show_progress(
                step_id="wandsender",
                progress_action="wandsender_waiting",
                progress_task=self._wandsender_task,
                description_placeholders=self._wandsender_placeholders(),
            )

        # --- Re-entry after Phase "waiting": stick found (or timeout/error) ---
        if self._wandsender_phase == "waiting":
            # Spurious re-entry guard: HA re-runs the step whenever the progress
            # is (re)shown, not only when the wait task finishes. Only evaluate
            # the result once the task is actually done; otherwise keep showing
            # the same progress so we don't tear down the listener prematurely.
            if self._wandsender_task and not self._wandsender_task.done():
                return self.async_show_progress(
                    step_id="wandsender",
                    progress_action="wandsender_waiting",
                    progress_task=self._wandsender_task,
                    description_placeholders=self._wandsender_placeholders(),
                )
            _LOGGER.debug(
                "WANDSENDER waiting re-entry: error=%s stick_found_event=%s",
                self._result_holder.get("error"),
                self._stick_found_event.is_set() if self._stick_found_event else None,
            )
            if self._result_holder.get("error"):
                self._wandsender_abort_reason = "cannot_connect"
                await self._cleanup_serial()
                return self.async_show_progress_done(next_step_id="wandsender_failed")
            if not (self._stick_found_event and self._stick_found_event.is_set()):
                # Timed out waiting for the Handsender.
                self._wandsender_abort_reason = "pairing_timeout"
                await self._cleanup_serial()
                return self.async_show_progress_done(next_step_id="wandsender_failed")

            # Stick identified – ask the user to press STOP, wait for params.
            self._wandsender_phase = "press_stop"
            self._wandsender_task = self.hass.async_create_task(
                asyncio.to_thread(self._params_captured_event.wait, _PAIRING_TIMEOUT)
            )
            return self.async_show_progress(
                step_id="wandsender",
                progress_action="wandsender_press_stop",
                progress_task=self._wandsender_task,
                description_placeholders=self._wandsender_placeholders(),
            )

        # --- Re-entry after Phase "press_stop": params captured (or timeout) ---
        if self._wandsender_phase == "press_stop":
            # Spurious re-entry guard (see "waiting"): a re-run while the params
            # task is still pending must NOT be treated as a timeout. This was
            # the root cause of the "Invalid flow specified" abort: HA re-ran the
            # step ~30 ms after we showed the press_stop progress, the code
            # assumed the wait had finished, called _cleanup_serial() (closing
            # the port and killing the listener) and aborted the flow.
            if self._wandsender_task and not self._wandsender_task.done():
                return self.async_show_progress(
                    step_id="wandsender",
                    progress_action="wandsender_press_stop",
                    progress_task=self._wandsender_task,
                    description_placeholders=self._wandsender_placeholders(),
                )
            if not self._result_holder.get("params"):
                self._wandsender_abort_reason = (
                    "cannot_connect"
                    if self._result_holder.get("error")
                    else "pairing_timeout"
                )
                await self._cleanup_serial()
                return self.async_show_progress_done(next_step_id="wandsender_failed")

            # Params captured – stop the listener, store params, reconnect & scan.
            params = self._result_holder["params"]
            await self._cleanup_serial()
            self._user_input[CONF_CHANNEL] = params[CONF_CHANNEL]
            self._user_input[CONF_PAN_ID] = params[CONF_PAN_ID]
            self._user_input[CONF_NETWORK_KEY] = params[CONF_NETWORK_KEY]

            self._wandsender_phase = "connecting"
            self._wandsender_task = self.hass.async_create_task(
                self._wandsender_connect_and_scan()
            )
            return self.async_show_progress(
                step_id="wandsender",
                progress_action="wandsender_connecting",
                progress_task=self._wandsender_task,
                description_placeholders=self._wandsender_placeholders(),
            )

        # --- Re-entry after Phase "connecting": scan done ---
        # Spurious re-entry guard (see "waiting"): keep showing progress until
        # the connect+scan task has actually finished.
        if self._wandsender_task and not self._wandsender_task.done():
            return self.async_show_progress(
                step_id="wandsender",
                progress_action="wandsender_connecting",
                progress_task=self._wandsender_task,
                description_placeholders=self._wandsender_placeholders(),
            )
        if self._result_holder.get("connect_error"):
            self._wandsender_abort_reason = "cannot_connect"
            return self.async_show_progress_done(next_step_id="wandsender_failed")

        return self.async_show_progress_done(next_step_id="devices")

    async def _wandsender_connect_and_scan(self) -> None:
        """Reconnect with the captured network params and scan for blinds.

        Runs as the Phase "connecting" progress task. Stores the discovered
        devices on ``self._discovered_devices`` or records a connect error.
        """
        try:
            result = await _test_connection(
                self.hass,
                self._user_input[CONF_SERIAL_PORT],
                self._user_input[CONF_CHANNEL],
                self._user_input[CONF_PAN_ID],
                self._user_input[CONF_NETWORK_KEY],
            )
            self._discovered_devices = result.get("devices", [])
        except CannotConnect as exc:
            _LOGGER.error("Failed to connect after Wandsender pairing: %s", exc)
            self._result_holder["connect_error"] = str(exc)

    async def async_step_wandsender_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Abort the flow after a Wandsender pairing failure or timeout."""
        return self.async_abort(
            reason=self._wandsender_abort_reason or "pairing_timeout"
        )

    def _wandsender_placeholders(self) -> dict[str, str]:
        """Return description placeholders for the wandsender progress dialog."""
        params = self._result_holder.get("params") or {}
        snr = self._result_holder.get("snr")
        return {
            "snr": str(snr) if snr else "—",
            "channel": str(params.get(CONF_CHANNEL, "—")),
            "pan_id": params.get(CONF_PAN_ID, "—"),
            "network_key": params.get(CONF_NETWORK_KEY, "—"),
        }

    # ------------------------------------------------------------------
    # Step 3c: New network creation
    # ------------------------------------------------------------------

    async def async_step_new_network(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 3c: Generate a new network and wait for a motor to join.

        On first call: generate credentials, open serial port, start listener.
        On subsequent calls: check if a motor has joined.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            # User clicked Submit – check if a motor joined
            if self._result_holder.get("snr") is not None:
                await self._cleanup_serial()
                # Credentials were already stored in _user_input on first call

                # Connect and scan for devices
                try:
                    result = await _test_connection(
                        self.hass,
                        self._user_input[CONF_SERIAL_PORT],
                        self._user_input[CONF_CHANNEL],
                        self._user_input[CONF_PAN_ID],
                        self._user_input[CONF_NETWORK_KEY],
                    )
                    self._discovered_devices = result.get("devices", [])
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                    return self.async_show_form(
                        step_id="new_network",
                        errors=errors,
                        description_placeholders=self._new_network_placeholders(),
                    )

                if self._discovered_devices:
                    return await self.async_step_devices()
                return self._create_entry()

            elif self._result_holder.get("error"):
                errors["base"] = "cannot_connect"
                await self._cleanup_serial()
            else:
                errors["base"] = "new_network_waiting"

            return self.async_show_form(
                step_id="new_network",
                errors=errors,
                description_placeholders=self._new_network_placeholders(),
            )

        # First call: generate credentials and start listener
        pan_id = self._generate_pan_id()
        network_key = self._generate_network_key()
        channel = DEFAULT_NEW_NETWORK_CHANNEL

        self._user_input[CONF_CHANNEL] = channel
        self._user_input[CONF_PAN_ID] = pan_id
        self._user_input[CONF_NETWORK_KEY] = network_key

        port = self._user_input[CONF_SERIAL_PORT]
        try:
            self._serial_obj = await self.hass.async_add_executor_job(
                _init_stick_probe, port, channel, pan_id, network_key
            )
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.error("Failed to open serial port for new network: %s", exc)
            return self.async_show_form(
                step_id="new_network",
                errors={"base": "cannot_connect"},
                description_placeholders=self._new_network_placeholders(),
            )

        self._result_holder = {}
        self._stop_event = threading.Event()
        self._listen_thread = threading.Thread(
            target=_listen_for_motor_join,
            args=(self._serial_obj, pan_id, self._stop_event, self._result_holder),
            daemon=True,
            name="wms-new-network-listener",
        )
        self._listen_thread.start()

        return self.async_show_form(
            step_id="new_network",
            description_placeholders=self._new_network_placeholders(),
        )

    def _new_network_placeholders(self) -> dict[str, str]:
        """Return description placeholders for the new_network step."""
        channel = self._user_input.get(CONF_CHANNEL, DEFAULT_NEW_NETWORK_CHANNEL)
        pan_id = self._user_input.get(CONF_PAN_ID, "—")
        network_key = self._user_input.get(CONF_NETWORK_KEY, "—")
        snr = self._result_holder.get("snr")
        if snr is not None:
            status = f"✅ Motor joined! SNR: {snr}"
        else:
            status = "⏳ Waiting for motor to join…"
        return {
            "channel": str(channel),
            "pan_id": pan_id,
            "network_key": network_key,
            "status": status,
        }

    # ------------------------------------------------------------------
    # Step 4: device selection
    # ------------------------------------------------------------------

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 4: Select which discovered blinds to add."""
        blind_devices = [
            d
            for d in self._discovered_devices
            if d.get("device_type", "") in SUPPORTED_DEVICE_TYPES
        ]

        if user_input is not None:
            selected_snrs = user_input.get("selected_devices", [])
            if selected_snrs:
                selected_devices = [
                    d
                    for d in self._discovered_devices
                    if str(d.get("snr", "")) in selected_snrs
                ]
            else:
                # No explicit selection → add all discovered blinds. This keeps
                # CONF_DEVICES populated so the sensor/binary_sensor platforms
                # (which have no scan fallback) also create their entities on
                # first setup, not only after a later rescan.
                selected_devices = blind_devices
            self._user_input[CONF_DEVICES] = selected_devices
            return self._create_entry()

        if not blind_devices:
            self._user_input[CONF_DEVICES] = []
            return self._create_entry()

        # Prefer the product type name ("PergolaAwning") over the actuator
        # hardware name ("Plug receiver") when we managed to detect it, so the
        # user picks the right device by what's actually on their facade.
        def _label(d: dict) -> str:
            product = d.get("product_type_str")
            primary = product or d.get("device_type_str", "?")
            return f"{primary} - SNR {d['snr']} ({d['snr_hex']})"

        device_options = {str(d["snr"]): _label(d) for d in blind_devices}

        schema = vol.Schema(
            {
                vol.Optional(
                    "selected_devices", default=list(device_options)
                ): cv.multi_select(device_options),
            }
        )

        return self.async_show_form(
            step_id="devices",
            data_schema=schema,
            description_placeholders={
                "device_count": str(len(blind_devices)),
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_entry(self) -> FlowResult:
        """Create the config entry."""
        port = self._user_input[CONF_SERIAL_PORT]
        return self.async_create_entry(
            title=f"Warema WMS ({port})",
            data=self._user_input,
        )

    async def _cleanup_serial(self) -> None:
        """Stop the background listener thread and close the serial port."""
        _LOGGER.debug(
            "WANDSENDER _cleanup_serial called (phase=%s)", self._wandsender_phase
        )
        if self._stop_event:
            self._stop_event.set()
            self._stop_event = None
        if self._listen_thread:
            await self.hass.async_add_executor_job(self._listen_thread.join, 2.0)
            self._listen_thread = None
        if self._serial_obj:
            try:
                await self.hass.async_add_executor_job(self._serial_obj.close)
            except Exception:  # pylint: disable=broad-except
                pass
            self._serial_obj = None

    @staticmethod
    def _generate_pan_id() -> str:
        """Generate a random 4-char hex PAN ID (excluding FFFF)."""
        while True:
            pan_id = format(random.randint(0x0001, 0xFFFE), "04X")
            if pan_id != "FFFF":
                return pan_id

    @staticmethod
    def _generate_network_key() -> str:
        """Generate a random 32-char hex network key (128-bit)."""
        return format(random.getrandbits(128), "032X")


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


class WaremaWmsOptionsFlow(config_entries.OptionsFlow):
    """Handle options for Warema WMS.

    Two flows are offered from the init menu:
      1. **Rescan** the live network for new blinds (no re-pairing needed)
      2. **Configure firmware parameters** of an existing motor / actuator.
         These are the persistent on-device settings WMS Studio Pro exposes
         under "Position bei manueller Bedienung" etc. - they survive power
         cycles and apply to handheld remote operation too.
    """

    def __init__(self) -> None:
        """Initialize options flow.

        ``self.config_entry`` is provided automatically by the OptionsFlow base
        class (read-only property), so it must not be set here.
        """
        self._discovered_devices: list[dict] = []
        self._fw_selected_snr: int | None = None
        self._fw_current_params = None  # MotorParameters when read succeeds
        self._fw_source_snr: int | None = None  # chosen source, None = own values
        self._fw_source_name: str = ""  # display name of the source for the spinner
        self._fw_prefill = None  # MotorParameters used to fill the form
        self._fw_load_task = None  # asyncio task of the parameter read
        self._fw_ref_failed = False  # source unreadable -> warn in the form
        self._ds_selected_snr: int | None = None  # device_settings: chosen device
        # Used by pair_device (Wandsender-bound device capture)
        self._serial_obj: serial.Serial | None = None
        self._stop_event: threading.Event | None = None
        self._listen_thread: threading.Thread | None = None
        self._result_holder: dict = {}
        self._stick_found_event: threading.Event | None = None
        self._device_found_event: threading.Event | None = None
        self._pair_phase: str | None = None
        self._pair_abort_reason: str | None = None
        self._pair_task: asyncio.Task | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Entry point: show a menu choosing rescan vs. firmware config."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "rescan",
                "pair_device",
                "configure_firmware",
                "device_settings",
            ],
        )

    # ------------------------------------------------------------------
    # Rescan for new devices
    # ------------------------------------------------------------------

    async def async_step_rescan(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Scan the live network and offer blinds that aren't configured yet.

        Reuses the running coordinator's stick connection, so the serial port
        is not opened a second time and no re-pairing is required.
        """
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None:
            return self.async_abort(reason="not_loaded")

        scanned = await coordinator.async_scan_devices(auto_assign=False)

        existing_snrs = {
            int(d["snr"])
            for d in self.config_entry.data.get(CONF_DEVICES, [])
            if d.get("snr") is not None
        }
        self._discovered_devices = [
            d
            for d in scanned
            if d.get("device_type", "") in SUPPORTED_DEVICE_TYPES
            and d.get("snr") is not None
            and int(d["snr"]) not in existing_snrs
        ]

        if not self._discovered_devices:
            return self.async_abort(reason="no_new_devices")

        return await self.async_step_select_devices()

    # ------------------------------------------------------------------
    # Pair a device that only answers via its Wandsender
    # ------------------------------------------------------------------
    #
    # Some actuators - observed on a Warema Lamaxa slat-roof's integrated
    # lighting, see https://github.com/mike-goldfinger/ha-warema-wms/issues/8 -
    # never joined the WMS network on their own. They are only ever paired to
    # a handheld Wandsender and so never answer the broadcast scanRequest that
    # "rescan" relies on, even though the Wandsender itself controls them fine.
    # Such a device can be revealed by talking to the Wandsender instead of
    # the device directly; this flow reproduces that by putting the
    # Wandsender in pairing mode against our stick (using the network's real
    # PAN ID, so no re-keying happens) and waiting for the paired device to
    # announce itself.

    async def async_step_pair_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Wrapper that logs any exception raised while pairing.

        Mirrors the config-flow wandsender step: HA drops a flow whose step
        raises, replacing it with the generic "Invalid flow specified", so the
        real cause is logged here via the integration's own logger first.
        """
        try:
            return await self._pair_device_impl(user_input)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("PAIR_DEVICE step crashed (phase=%s)", self._pair_phase)
            raise

    async def _pair_device_impl(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Listen for a Wandsender-paired device using a live progress dialog.

        Phases (see ``_listen_for_paired_device`` for the wire-level details):

          * "waiting"      - wait for the Wandsender to identify our stick
                              (waveRequest).
          * "scanning"     - stick identified; ask the user to trigger the
                              paired device from the Wandsender so it
                              announces itself (joinNetworkRequest).
          * "rescanning"   - device SNR captured; reconnect the coordinator
                              and scan so we get its device_type/product_type.

        Home Assistant re-invokes this step whenever the current
        ``progress_task`` finishes, so each call advances to the next phase.
        """
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None:
            return self.async_abort(reason="not_loaded")

        if self._pair_phase is None:
            # First entry: release the coordinator's serial port so we can
            # open it ourselves, then start the listener with the network's
            # real PAN ID (we are not capturing a new key, just nudging an
            # already-known Wandsender into revealing its paired device).
            port = self.config_entry.data[CONF_SERIAL_PORT]
            channel = self.config_entry.data[CONF_CHANNEL]
            pan_id = self.config_entry.data[CONF_PAN_ID]
            key = self.config_entry.data[CONF_NETWORK_KEY]

            await coordinator.async_disconnect()

            try:
                self._serial_obj = await self.hass.async_add_executor_job(
                    _init_stick_probe, port, channel, pan_id, key
                )
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.error(
                    "Failed to open serial port for pair_device: %s", exc
                )
                await coordinator.async_connect()
                return self.async_abort(reason="cannot_connect")

            self._result_holder = {}
            self._stop_event = threading.Event()
            self._stick_found_event = threading.Event()
            self._device_found_event = threading.Event()
            self._listen_thread = threading.Thread(
                target=_listen_for_paired_device,
                args=(
                    self._serial_obj,
                    pan_id,
                    self._stop_event,
                    self._result_holder,
                    self._stick_found_event,
                    self._device_found_event,
                ),
                daemon=True,
                name="wms-pair-device-listener",
            )
            self._listen_thread.start()

            self._pair_phase = "waiting"
            self._pair_task = self.hass.async_create_task(
                asyncio.to_thread(self._stick_found_event.wait, _PAIRING_TIMEOUT)
            )
            return self.async_show_progress(
                step_id="pair_device",
                progress_action="pair_device_waiting",
                progress_task=self._pair_task,
                description_placeholders=self._pair_placeholders(),
            )

        if self._pair_phase == "waiting":
            if self._pair_task and not self._pair_task.done():
                return self.async_show_progress(
                    step_id="pair_device",
                    progress_action="pair_device_waiting",
                    progress_task=self._pair_task,
                    description_placeholders=self._pair_placeholders(),
                )
            if self._result_holder.get("error"):
                self._pair_abort_reason = "cannot_connect"
                await self._pair_cleanup_serial(coordinator)
                return self.async_show_progress_done(next_step_id="pair_device_failed")
            if not (self._stick_found_event and self._stick_found_event.is_set()):
                self._pair_abort_reason = "pairing_timeout"
                await self._pair_cleanup_serial(coordinator)
                return self.async_show_progress_done(next_step_id="pair_device_failed")

            # Stick identified - now wait for the paired device to announce
            # itself once the user triggers it from the Wandsender.
            self._pair_phase = "scanning"
            self._pair_task = self.hass.async_create_task(
                asyncio.to_thread(self._device_found_event.wait, _PAIRING_TIMEOUT)
            )
            return self.async_show_progress(
                step_id="pair_device",
                progress_action="pair_device_scanning",
                progress_task=self._pair_task,
                description_placeholders=self._pair_placeholders(),
            )

        if self._pair_phase == "scanning":
            if self._pair_task and not self._pair_task.done():
                return self.async_show_progress(
                    step_id="pair_device",
                    progress_action="pair_device_scanning",
                    progress_task=self._pair_task,
                    description_placeholders=self._pair_placeholders(),
                )
            if not self._result_holder.get("device_snr_hex"):
                self._pair_abort_reason = (
                    "cannot_connect"
                    if self._result_holder.get("error")
                    else "pairing_timeout"
                )
                await self._pair_cleanup_serial(coordinator)
                return self.async_show_progress_done(next_step_id="pair_device_failed")

            # Device SNR captured - close our probe port and reconnect the
            # coordinator, then do a normal scan to pick up its device_type /
            # product_type before offering it on the select_devices form.
            # Set the phase to "rescanning" BEFORE cleanup so its "coordinator
            # still disconnected" fallback reconnect is skipped - we start the
            # real reconnect ourselves right below instead of racing it.
            self._pair_phase = "rescanning"
            await self._pair_cleanup_serial(coordinator)
            self._pair_task = self.hass.async_create_task(coordinator.async_connect())
            return self.async_show_progress(
                step_id="pair_device",
                progress_action="pair_device_reconnecting",
                progress_task=self._pair_task,
                description_placeholders=self._pair_placeholders(),
            )

        # --- Phase "rescanning": coordinator reconnected, now scan ---
        if self._pair_task and not self._pair_task.done():
            return self.async_show_progress(
                step_id="pair_device",
                progress_action="pair_device_reconnecting",
                progress_task=self._pair_task,
                description_placeholders=self._pair_placeholders(),
            )
        if self._pair_task and self._pair_task.exception() is not None:
            _LOGGER.error(
                "Failed to reconnect coordinator after pair_device: %s",
                self._pair_task.exception(),
            )
            self._pair_abort_reason = "cannot_connect"
            return self.async_show_progress_done(next_step_id="pair_device_failed")

        device_snr = self._result_holder.get("device_snr")
        scanned = await coordinator.async_scan_devices(auto_assign=False)
        existing_snrs = {
            int(d["snr"])
            for d in self.config_entry.data.get(CONF_DEVICES, [])
            if d.get("snr") is not None
        }
        self._discovered_devices = [
            d
            for d in scanned
            if d.get("device_type", "") in SUPPORTED_DEVICE_TYPES
            and d.get("snr") is not None
            and int(d["snr"]) not in existing_snrs
        ]

        if not self._discovered_devices:
            # The device answered the Wandsender's join but not our broadcast
            # scan afterwards (e.g. it went back to sleep) - tell the user its
            # SNR was captured so they know pairing itself worked.
            self._pair_abort_reason = "device_not_in_scan"
            return self.async_show_progress_done(next_step_id="pair_device_failed")

        # Put the freshly paired device first in the selection list.
        self._discovered_devices.sort(
            key=lambda d: 0 if int(d.get("snr", -1)) == device_snr else 1
        )
        return self.async_show_progress_done(next_step_id="select_devices")

    async def _pair_cleanup_serial(self, coordinator) -> None:
        """Stop the pairing listener, close our probe port, reconnect the coordinator.

        Safe to call multiple times; only performs work once per invocation of
        the flow (subsequent calls act on already-cleared state).
        """
        if self._stop_event:
            self._stop_event.set()
        if self._listen_thread and self._listen_thread.is_alive():
            await self.hass.async_add_executor_job(self._listen_thread.join, 2.0)
        self._listen_thread = None
        if self._serial_obj:
            try:
                await self.hass.async_add_executor_job(self._serial_obj.close)
            except Exception:  # pylint: disable=broad-except
                pass
            self._serial_obj = None
        # Reconnect the coordinator so the integration keeps working even if
        # pairing failed/timed out. The "rescanning" phase already reconnects
        # explicitly on the success path, so avoid double-connecting.
        if coordinator.stick is None and self._pair_phase != "rescanning":
            try:
                await coordinator.async_connect()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception(
                    "Failed to reconnect coordinator after pair_device cleanup"
                )

    async def async_step_pair_device_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Abort the flow after a pair_device failure or timeout."""
        return self.async_abort(reason=self._pair_abort_reason or "pairing_timeout")

    def _pair_placeholders(self) -> dict[str, str]:
        """Return description placeholders for the pair_device progress dialog."""
        snr = self._result_holder.get("snr")
        device_snr = self._result_holder.get("device_snr")
        return {
            "snr": str(snr) if snr else "—",
            "device_snr": str(device_snr) if device_snr else "—",
        }

    async def async_step_select_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user pick which newly found blinds to add."""
        if user_input is not None:
            selected = user_input.get("selected_devices", [])
            new_devices = [
                d for d in self._discovered_devices if str(d.get("snr", "")) in selected
            ]
            if new_devices:
                merged = (
                    list(self.config_entry.data.get(CONF_DEVICES, [])) + new_devices
                )
                # Updating entry.data fires the update listener, which reloads
                # the integration so the new cover entities appear.
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={**self.config_entry.data, CONF_DEVICES: merged},
                )
            return self.async_create_entry(
                title="", data=dict(self.config_entry.options)
            )

        device_options = {
            str(d["snr"]): (
                f"{d.get('product_type_str') or d.get('device_type_str', '?')}"
                f" - SNR {d['snr']} ({d['snr_hex']})"
            )
            for d in self._discovered_devices
        }
        return self.async_show_form(
            step_id="select_devices",
            data_schema=vol.Schema(
                {
                    vol.Optional("selected_devices"): cv.multi_select(device_options),
                }
            ),
            description_placeholders={
                "device_count": str(len(self._discovered_devices)),
            },
        )

    # ------------------------------------------------------------------
    # Per-device settings (HA-side position inversion)
    # ------------------------------------------------------------------

    async def async_step_device_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick a configured cover whose HA-side settings to edit."""
        devices = [
            d
            for d in self.config_entry.data.get(CONF_DEVICES, [])
            if d.get("device_type", "") in COVER_DEVICE_TYPES
            and d.get("snr") is not None
        ]
        if not devices:
            return self.async_abort(reason="no_configurable_devices")

        if user_input is not None:
            try:
                self._ds_selected_snr = int(user_input["device"])
            except (KeyError, ValueError, TypeError):
                return self.async_abort(reason="invalid_device")
            return await self.async_step_device_invert()

        device_options = {str(d["snr"]): self._device_label(d) for d in devices}
        return self.async_show_form(
            step_id="device_settings",
            data_schema=vol.Schema({vol.Required("device"): vol.In(device_options)}),
        )

    async def async_step_device_invert(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Toggle HA-side position inversion for the chosen device."""
        snr_key = str(self._ds_selected_snr)
        invert_map = dict(self.config_entry.options.get(OPT_INVERT_POSITION, {}))

        if user_input is not None:
            invert_map[snr_key] = bool(user_input.get(OPT_INVERT_POSITION, False))
            new_options = {
                **self.config_entry.options,
                OPT_INVERT_POSITION: invert_map,
            }
            # Persisting the options fires the update listener, which reloads the
            # integration so the cover picks up the new direction.
            return self.async_create_entry(title="", data=new_options)

        current = bool(invert_map.get(snr_key, False))
        return self.async_show_form(
            step_id="device_invert",
            data_schema=vol.Schema(
                {vol.Optional(OPT_INVERT_POSITION, default=current): bool}
            ),
        )

    # ------------------------------------------------------------------
    # Configure firmware parameters of an actuator (Block 38)
    # ------------------------------------------------------------------

    def _configurable_devices(self) -> list[dict]:
        """Return configured devices whose firmware parameters we can edit.

        Only Actuator UP (20) / Plug Receiver (21) / Actuator 230V UP (2E) store
        the manualOperation / scene / common params we expose. Radio motors (25)
        use a different parameter layout.

        Products that lay out block 38 differently (slat roofs, dimmers and the
        other switching products) are excluded as well: their parameters live at
        different addresses, so editing them here would write to the wrong ones.
        """
        from .pywarema.protocol import has_standard_param_layout

        devices = list(self.config_entry.data.get(CONF_DEVICES, []))
        return [
            d
            for d in devices
            if d.get("device_type", "") in {"20", "21", "2E"}
            and has_standard_param_layout(d.get("product_type"))
        ]

    def _device_label(self, d: dict) -> str:
        """Friendly label for a device, preferring the HA device-registry name."""
        from homeassistant.helpers import device_registry as dr

        registry = dr.async_get(self.hass)
        entry = registry.async_get_device(identifiers={(DOMAIN, d["snr_hex"])})
        friendly = entry.name_by_user if entry and entry.name_by_user else None
        if not friendly and entry:
            friendly = entry.name
        base = f"SNR {d['snr']} ({d['snr_hex']})"
        if (
            friendly
            and friendly != f"{d.get('device_type_str', '')} {d['snr']}".strip()
        ):
            return f"{friendly} - {base}"
        # Fallback: keep the original "<type> - SNR <n> (<hex>)" format.
        return f"{d.get('device_type_str', '?')} - {base}"

    async def async_step_configure_firmware(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick a device whose firmware parameters to configure."""
        configurable = self._configurable_devices()
        if not configurable:
            return self.async_abort(reason="no_configurable_devices")

        if user_input is not None:
            try:
                self._fw_selected_snr = int(user_input["device"])
            except (KeyError, ValueError, TypeError):
                return self.async_abort(reason="invalid_device")
            # Resolve the optional copy source. "" means "use the target's own
            # current values"; any SNR means load that device's values instead.
            copy_from = user_input.get("copy_from", "")
            if copy_from:
                try:
                    self._fw_source_snr = int(copy_from)
                except (ValueError, TypeError):
                    self._fw_source_snr = None
            else:
                self._fw_source_snr = None
            self._fw_source_name = self._source_display_name()
            # Reset transient load state so a fresh read happens.
            self._fw_load_task = None
            self._fw_prefill = None
            self._fw_ref_failed = False
            return await self.async_step_firmware_load()

        device_options = {str(d["snr"]): self._device_label(d) for d in configurable}
        source_options = {"": "— Aktuelle Werte des Geräts —"}
        source_options.update(
            {str(d["snr"]): self._device_label(d) for d in configurable}
        )
        return self.async_show_form(
            step_id="configure_firmware",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): vol.In(device_options),
                    vol.Optional("copy_from", default=""): vol.In(source_options),
                }
            ),
            description_placeholders={"device_count": str(len(configurable))},
        )

    def _source_display_name(self) -> str:
        """Human-readable name of the chosen copy source for the spinner text."""
        if self._fw_source_snr is None or self._fw_source_snr == self._fw_selected_snr:
            return "den aktuellen Werten des Geräts"
        match = next(
            (
                d
                for d in self._configurable_devices()
                if int(d["snr"]) == self._fw_source_snr
            ),
            None,
        )
        return self._device_label(match) if match else str(self._fw_source_snr)

    async def _async_load_fw_params(self) -> None:
        """Read the target's params (diff baseline) and, if requested, the source.

        The target's values are always read and kept as ``_fw_current_params`` —
        the write-diff baseline that is never overwritten. ``_fw_prefill`` is what
        seeds the form: the source device's values when copying, otherwise the
        target's own values.
        """
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None or coordinator.stick is None:
            self._fw_current_params = None
            return

        self._fw_current_params = await self.hass.async_add_executor_job(
            coordinator.stick.read_motor_parameters, self._fw_selected_snr
        )
        if self._fw_current_params is None:
            self._fw_prefill = None
            return

        if self._fw_source_snr and self._fw_source_snr != self._fw_selected_snr:
            ref = await self.hass.async_add_executor_job(
                coordinator.stick.read_motor_parameters, self._fw_source_snr
            )
            self._fw_ref_failed = ref is None
            self._fw_prefill = ref if ref is not None else self._fw_current_params
        else:
            self._fw_prefill = self._fw_current_params

    async def async_step_firmware_load(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show a spinner while reading motor parameters, then open the form."""
        if self._fw_load_task is None:
            self._fw_load_task = self.hass.async_create_task(
                self._async_load_fw_params()
            )

        if not self._fw_load_task.done():
            return self.async_show_progress(
                step_id="firmware_load",
                progress_action="loading",
                progress_task=self._fw_load_task,
                description_placeholders={"source_name": self._fw_source_name},
            )

        self._fw_load_task = None
        return self.async_show_progress_done(next_step_id="firmware_params")

    def _show_firmware_form(
        self, prefill, errors: dict[str, str] | None = None
    ) -> FlowResult:
        """Render the firmware-params form, pre-filled from ``prefill``.

        ``prefill`` is the MotorParameters object whose values seed the field
        defaults — either the target's own current values or a source device's
        values when the user requested a copy.
        """
        selected = next(
            (
                d
                for d in self._configurable_devices()
                if int(d["snr"]) == self._fw_selected_snr
            ),
            None,
        )
        device_name = self._device_label(selected) if selected else ""
        return self.async_show_form(
            step_id="firmware_params",
            data_schema=self._build_firmware_schema(prefill),
            errors=errors or {},
            description_placeholders={"device_name": device_name},
        )

    async def async_step_firmware_params(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Read current firmware parameters, present a form, write changes."""
        from .pywarema.protocol import MotorParameters

        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is None or coordinator.stick is None:
            return self.async_abort(reason="not_loaded")

        if user_input is None:
            # Parameters were already read by the firmware_load progress step.
            # `_fw_current_params` is the diff baseline (target's own values),
            # `_fw_prefill` is what seeds the form (target or copied source).
            if self._fw_current_params is None:
                return self.async_abort(reason="cannot_read_params")
            errors = {"base": "ref_unreachable"} if self._fw_ref_failed else None
            return self._show_firmware_form(self._fw_prefill, errors=errors)

        # Section selectors return their fields nested under the section key;
        # flatten everything into one dict before diffing.
        flat: dict[str, Any] = {}
        for key, value in user_input.items():
            if isinstance(value, dict):
                flat.update(value)
            else:
                flat[key] = value

        # Build a MotorParameters with only the changed fields and write.
        # Every field is present in `flat` with either the current value (user
        # left it alone) or a new value. We diff against the target baseline
        # `_fw_current_params` and write only what actually changed.
        cur = self._fw_current_params
        new = MotorParameters()

        def diff_int(field_key, current_val):
            v = flat.get(field_key)
            if v is None:
                return None
            v = int(v)
            return v if v != current_val else None

        def diff_bool(field_key, current_val):
            v = flat.get(field_key)
            if v is None:
                return None
            b = bool(v)
            return b if b != current_val else None

        def diff_float(field_key, current_val):
            v = flat.get(field_key)
            if v is None:
                return None
            f = float(v)
            return f if f != current_val else None

        new.manual_position = diff_int(
            "manual_position", cur.manual_position if cur else None
        )
        new.manual_angle = diff_int("manual_angle", cur.manual_angle if cur else None)
        new.manual_dwell_time = diff_int(
            "manual_dwell_time", cur.manual_dwell_time if cur else None
        )
        new.comfort_position = diff_int(
            "comfort_position", cur.comfort_position if cur else None
        )
        new.comfort_angle = diff_int(
            "comfort_angle", cur.comfort_angle if cur else None
        )
        new.comfort_auto_enabled = diff_bool(
            "comfort_auto_enabled", cur.comfort_auto_enabled if cur else None
        )
        new.absent_position = diff_int(
            "absent_position", cur.absent_position if cur else None
        )
        new.absent_angle = diff_int("absent_angle", cur.absent_angle if cur else None)
        new.run_time_up = diff_int("run_time_up", cur.run_time_up if cur else None)
        new.run_time_down = diff_int(
            "run_time_down", cur.run_time_down if cur else None
        )
        new.calibration_up = diff_int(
            "calibration_up", cur.calibration_up if cur else None
        )
        new.calibration_down = diff_int(
            "calibration_down", cur.calibration_down if cur else None
        )
        new.tilting_time = diff_float("tilting_time", cur.tilting_time if cur else None)
        new.min_angle = diff_int("min_angle", cur.min_angle if cur else None)
        new.max_angle = diff_int("max_angle", cur.max_angle if cur else None)
        new.tilting_steps = diff_int(
            "tilting_steps", cur.tilting_steps if cur else None
        )
        new.motor_rotation = diff_bool(
            "motor_rotation", cur.motor_rotation if cur else None
        )

        # Nothing changed -> close the dialog without contacting the device.
        if all(
            v is None
            for v in (
                new.manual_position,
                new.manual_angle,
                new.manual_dwell_time,
                new.comfort_position,
                new.comfort_angle,
                new.comfort_auto_enabled,
                new.absent_position,
                new.absent_angle,
                new.run_time_up,
                new.run_time_down,
                new.calibration_up,
                new.calibration_down,
                new.tilting_time,
                new.min_angle,
                new.max_angle,
                new.tilting_steps,
                new.motor_rotation,
            )
        ):
            return self.async_create_entry(
                title="", data=dict(self.config_entry.options)
            )

        ok = await self.hass.async_add_executor_job(
            coordinator.stick.write_motor_parameters, self._fw_selected_snr, new
        )
        if not ok:
            return self._show_firmware_form(cur, errors={"base": "write_failed"})

        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    @staticmethod
    def _build_firmware_schema(cur) -> vol.Schema:
        """Build the firmware-params form schema, prefilled from ``cur``.

        Numeric fields use ``NumberSelector`` in BOX mode (input fields, not
        sliders); booleans use a toggle. Related fields are grouped into
        collapsible sections. All fields are ``vol.Required`` with their current
        value as the default, so the user always sees the current state, edits
        what they want, and the submit handler diffs to decide what to write.
        ``cur`` may be the target's own values or a copied source device's values.
        """

        def num(lo, hi, step=1, unit=None) -> NumberSelector:
            config: dict[str, Any] = {
                "min": lo,
                "max": hi,
                "step": step,
                "mode": NumberSelectorMode.BOX,
            }
            # HA's selector schema validates unit_of_measurement as a str, so a
            # None value raises vol.Invalid; only set it when a unit is given.
            if unit is not None:
                config["unit_of_measurement"] = unit
            return NumberSelector(NumberSelectorConfig(**config))

        def dflt(value, fallback):
            return value if value is not None else fallback

        def sect(schema: dict) -> section:
            return section(vol.Schema(schema), {"collapsed": True})

        return vol.Schema(
            {
                vol.Required("manual"): sect(
                    {
                        vol.Required(
                            "manual_position",
                            default=dflt(cur.manual_position, 100),
                        ): num(0, 100, unit="%"),
                        vol.Required(
                            "manual_angle", default=dflt(cur.manual_angle, 0)
                        ): num(0, 75, unit="°"),
                        vol.Required(
                            "manual_dwell_time",
                            default=dflt(cur.manual_dwell_time, 0),
                        ): num(0, 254),
                    }
                ),
                vol.Required("comfort"): sect(
                    {
                        vol.Required(
                            "comfort_position",
                            default=dflt(cur.comfort_position, 50),
                        ): num(0, 100, unit="%"),
                        vol.Required(
                            "comfort_angle", default=dflt(cur.comfort_angle, 0)
                        ): num(0, 75, unit="°"),
                        vol.Required(
                            "comfort_auto_enabled",
                            default=dflt(cur.comfort_auto_enabled, True),
                        ): BooleanSelector(),
                    }
                ),
                vol.Required("absent"): sect(
                    {
                        vol.Required(
                            "absent_position",
                            default=dflt(cur.absent_position, 95),
                        ): num(0, 100, unit="%"),
                        vol.Required(
                            "absent_angle", default=dflt(cur.absent_angle, 75)
                        ): num(0, 75, unit="°"),
                    }
                ),
                vol.Required("travel"): sect(
                    {
                        vol.Required(
                            "run_time_up", default=dflt(cur.run_time_up, 90)
                        ): num(0, 254, unit="s"),
                        vol.Required(
                            "run_time_down", default=dflt(cur.run_time_down, 90)
                        ): num(0, 254, unit="s"),
                        vol.Required(
                            "calibration_up", default=dflt(cur.calibration_up, 3)
                        ): num(0, 254, unit="s"),
                        vol.Required(
                            "calibration_down",
                            default=dflt(cur.calibration_down, 1),
                        ): num(0, 254, unit="s"),
                    }
                ),
                vol.Required("slats"): sect(
                    {
                        vol.Required(
                            "tilting_time", default=dflt(cur.tilting_time, 1.6)
                        ): num(0, 50.8, step=0.2, unit="s"),
                        vol.Required(
                            "min_angle", default=dflt(cur.min_angle, -75)
                        ): num(-127, 127, unit="°"),
                        vol.Required("max_angle", default=dflt(cur.max_angle, 75)): num(
                            -127, 127, unit="°"
                        ),
                        vol.Required(
                            "tilting_steps", default=dflt(cur.tilting_steps, 10)
                        ): num(0, 254),
                    }
                ),
                vol.Required("misc"): sect(
                    {
                        vol.Required(
                            "motor_rotation",
                            default=dflt(cur.motor_rotation, False),
                        ): BooleanSelector(),
                    }
                ),
            }
        )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
