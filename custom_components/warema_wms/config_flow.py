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
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    BLIND_DEVICE_TYPES,
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
            if d.get("device_type", "") in BLIND_DEVICE_TYPES
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

        device_options = {
            str(d["snr"]): f"{d['device_type_str']} - SNR {d['snr']} ({d['snr_hex']})"
            for d in blind_devices
        }

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

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Entry point: show a menu choosing rescan vs. firmware config."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["rescan", "configure_firmware"],
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
            if d.get("device_type", "") in BLIND_DEVICE_TYPES
            and d.get("snr") is not None
            and int(d["snr"]) not in existing_snrs
        ]

        if not self._discovered_devices:
            return self.async_abort(reason="no_new_devices")

        return await self.async_step_select_devices()

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
            str(d["snr"]): f"{d['device_type_str']} - SNR {d['snr']} ({d['snr_hex']})"
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
    # Configure firmware parameters of an actuator (Block 38)
    # ------------------------------------------------------------------

    async def async_step_configure_firmware(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pick a device whose firmware parameters to configure."""
        devices = list(self.config_entry.data.get(CONF_DEVICES, []))
        # Only Actuator UP (20) / Plug Receiver (21) / Actuator 230V UP (2E)
        # store the manualOperation / scene / common params we expose. Radio
        # motors (25) use a different parameter layout.
        configurable = [
            d
            for d in devices
            if d.get("device_type", "") in {"20", "21", "2E"}
        ]
        if not configurable:
            return self.async_abort(reason="no_configurable_devices")

        if user_input is not None:
            try:
                self._fw_selected_snr = int(user_input["device"])
            except (KeyError, ValueError, TypeError):
                return self.async_abort(reason="invalid_device")
            return await self.async_step_firmware_params()

        # Prefer the user-given friendly name from HA's device registry over
        # the scan-time default (which is just "<Device Type> <SNR>").
        from homeassistant.helpers import device_registry as dr

        registry = dr.async_get(self.hass)

        def label_for(d: dict) -> str:
            entry = registry.async_get_device(
                identifiers={(DOMAIN, d["snr_hex"])}
            )
            friendly = entry.name_by_user if entry and entry.name_by_user else None
            if not friendly and entry:
                friendly = entry.name
            base = f"SNR {d['snr']} ({d['snr_hex']})"
            if friendly and friendly != f"{d.get('device_type_str', '')} {d['snr']}".strip():
                return f"{friendly} - {base}"
            # Fallback: keep the original "<type> - SNR <n> (<hex>)" format.
            return f"{d.get('device_type_str', '?')} - {base}"

        device_options = {str(d["snr"]): label_for(d) for d in configurable}
        return self.async_show_form(
            step_id="configure_firmware",
            data_schema=vol.Schema(
                {vol.Required("device"): vol.In(device_options)}
            ),
            description_placeholders={"device_count": str(len(configurable))},
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
            # First entry: read current values from the device.
            self._fw_current_params = await self.hass.async_add_executor_job(
                coordinator.stick.read_motor_parameters, self._fw_selected_snr
            )
            if self._fw_current_params is None:
                return self.async_abort(reason="cannot_read_params")

            return self.async_show_form(
                step_id="firmware_params",
                data_schema=self._build_firmware_schema(self._fw_current_params),
                description_placeholders=self._firmware_placeholders(self._fw_current_params),
            )

        # Submit: build a MotorParameters with only the changed fields and write.
        # With the `vol.Required` schema, every field is present in user_input
        # with either the current value (user left it alone) or a new value.
        # We diff against `_fw_current_params` and write only what actually changed.
        cur = self._fw_current_params
        new = MotorParameters()

        def diff_int(field_key, current_val):
            v = user_input.get(field_key)
            if v is None:
                return None
            v = int(v)
            return v if v != current_val else None

        new.manual_position = diff_int("manual_position", cur.manual_position if cur else None)
        new.manual_angle = diff_int("manual_angle", cur.manual_angle if cur else None)
        new.manual_dwell_time = diff_int("manual_dwell_time", cur.manual_dwell_time if cur else None)
        new.comfort_position = diff_int("comfort_position", cur.comfort_position if cur else None)
        new.comfort_angle = diff_int("comfort_angle", cur.comfort_angle if cur else None)

        ia = user_input.get("is_absent")
        if ia is not None:
            ia = bool(ia)
            if cur is None or ia != cur.is_absent:
                new.is_absent = ia

        # Nothing changed -> close the dialog without contacting the device.
        if all(
            v is None
            for v in (
                new.manual_position,
                new.manual_angle,
                new.manual_dwell_time,
                new.comfort_position,
                new.comfort_angle,
                new.is_absent,
            )
        ):
            return self.async_create_entry(title="", data=dict(self.config_entry.options))

        ok = await self.hass.async_add_executor_job(
            coordinator.stick.write_motor_parameters, self._fw_selected_snr, new
        )
        if not ok:
            return self.async_show_form(
                step_id="firmware_params",
                data_schema=self._build_firmware_schema(cur),
                description_placeholders=self._firmware_placeholders(cur),
                errors={"base": "write_failed"},
            )

        return self.async_create_entry(title="", data=dict(self.config_entry.options))

    @staticmethod
    def _build_firmware_schema(cur) -> vol.Schema:
        """Build the firmware-params form schema, prefilled with current values.

        All fields are ``vol.Required`` with their current value as the default
        so the user always sees the current state. No checkbox/optional toggles
        - users edit what they want to change, leave the rest alone, and the
        submit handler diffs to decide what to actually write. This mirrors the
        ``STEP_MANUAL_SCHEMA`` pattern used elsewhere in this file.
        """
        # Defaults: prefer the read value, fall back to a sensible default if
        # the device reports the 0xFF "unset" sentinel (mapped to None).
        return vol.Schema(
            {
                vol.Required(
                    "manual_position",
                    default=cur.manual_position if cur.manual_position is not None else 100,
                ): vol.All(int, vol.Range(min=0, max=100)),
                vol.Required(
                    "manual_angle",
                    default=cur.manual_angle if cur.manual_angle is not None else 0,
                ): vol.All(int, vol.Range(min=0, max=75)),
                vol.Required(
                    "manual_dwell_time",
                    default=cur.manual_dwell_time if cur.manual_dwell_time is not None else 0,
                ): vol.All(int, vol.Range(min=0, max=254)),
                vol.Required(
                    "comfort_position",
                    default=cur.comfort_position if cur.comfort_position is not None else 50,
                ): vol.All(int, vol.Range(min=0, max=100)),
                vol.Required(
                    "comfort_angle",
                    default=cur.comfort_angle if cur.comfort_angle is not None else 0,
                ): vol.All(int, vol.Range(min=0, max=75)),
                vol.Required(
                    "is_absent",
                    default=bool(cur.is_absent) if cur.is_absent is not None else False,
                ): bool,
            }
        )

    @staticmethod
    def _firmware_placeholders(cur) -> dict[str, str]:
        """Show the current values in the form description."""
        def fmt(v, unit=""):
            return f"{v}{unit}" if v is not None else "—"
        return {
            "current_manual_position": fmt(cur.manual_position, " %"),
            "current_manual_angle": fmt(cur.manual_angle, "°"),
            "current_manual_dwell_time": fmt(cur.manual_dwell_time),
            "current_comfort_position": fmt(cur.comfort_position, " %"),
            "current_comfort_angle": fmt(cur.comfort_angle, "°"),
            "current_is_absent": "ja" if cur.is_absent else "nein" if cur.is_absent is not None else "—",
        }


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
