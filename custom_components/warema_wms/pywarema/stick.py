"""
WmsStick - Python port of the WMS Stick controller.

Ported from JavaScript wms-vb-stick.js and wms-vb-stick-usb.js.

This module provides the WmsStick class which manages:
  - Serial port communication with the WMS USB Stick (FTDI FT232R)
  - Message queue with timeout/retry logic
  - Blind (cover) device management
  - Position polling
  - Scan for devices on the WMS network

Usage:
    stick = WmsStick(
        port="/dev/ttyUSB1",
        channel=17,
        pan_id="ABCD",
        key="1234567890ABCDEF0123456789ABCDEF",
        callback=my_callback,
    )
    await stick.connect()
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import serial

from .protocol import (
    BLIND_DEVICE_TYPES,
    decode_frame,
    encode_cmd,
    snr_hex_to_num,
    snr_num_to_hex,
)

_LOGGER = logging.getLogger(__name__)

# Delay between processing queued messages (ms → seconds)
DELAY_MSG_PROC = 0.005  # 5 ms

# Timeouts and retries (from JS wmsMsgNew switch)
CMD_SETTINGS = {
    "blindGetPos": {"timeout": 0.500, "delay_after": 0.100, "retry": 5},
    "blindMoveToPos": {"timeout": 0.500, "delay_after": 0.300, "retry": 3},
    "blindStopMove": {"timeout": 0.200, "delay_after": 0.005, "retry": 3},
    "waveRequest": {"timeout": 0.500, "delay_after": 0.300, "retry": -1},
    "scanRequest": {"timeout": 0.750, "delay_after": 0.000, "retry": -1},
}
DEFAULT_TIMEOUT = 2.0
DEFAULT_RETRY = -1  # -1 = no retry

# Retry count for background position polls (pos-upd / watch-moving). The full
# CMD_SETTINGS retry (5) is meant for one-shot user queries; a periodic 5 s poll
# repeats anyway, so a single retry is enough. This keeps an unreachable motor
# from burning ~3 s of serial airtime (6 attempts) every cycle.
POS_POLL_RETRY = 1


@dataclass
class BlindPosition:
    """Represents the position of a blind."""

    pos: int = 0  # 0-100 (0=open, 100=closed)
    ang: int = 0  # -100 to +100
    moving: bool = False

    def equals(self, other: "BlindPosition") -> bool:
        """Check equality."""
        return (
            self.pos == other.pos
            and self.ang == other.ang
            and self.moving == other.moving
        )


@dataclass
class Blind:
    """Represents a WMS blind/cover device."""

    snr: int  # Integer serial number
    snr_hex: str  # 6-char hex (byte-reversed wire format)
    name: str
    pos_current: BlindPosition = field(
        default_factory=lambda: BlindPosition(-1, 0, False)
    )
    pos_requested: BlindPosition = field(
        default_factory=lambda: BlindPosition(0, 0, False)
    )
    device_type: str = "20"
    device_type_str: str = "Actuator UP"


class WmsMessage:
    """A queued WMS command message."""

    def __init__(self, cmd: str, snr, params: dict):
        """Initialize a WMS message.

        Args:
            cmd: Command name
            snr: Serial number (int or hex string)
            params: Command parameters
        """
        self.cmd = cmd
        if isinstance(snr, int):
            self.snr_num = snr
            self.snr = snr_num_to_hex(snr)
        else:
            self.snr = str(snr).upper().zfill(6)
            self.snr_num = snr_hex_to_num(self.snr)

        self.params = params
        self.stick_cmd = encode_cmd(cmd, self.snr, params)

        settings = CMD_SETTINGS.get(cmd, {})
        self.timeout = settings.get("timeout", DEFAULT_TIMEOUT)
        self.delay_after = settings.get("delay_after", 0.0)
        self.retry = settings.get("retry", DEFAULT_RETRY)
        self.on_end: Optional[Callable] = None
        self.queued_ts = time.time()
        self.com_ts: Optional[float] = None


class WmsStick:
    """Controls a Warema WMS USB Stick over serial port.

    Manages the message queue, serial communication, and blind state.
    Runs a background thread for serial I/O.

    Callback signature: callback(error: str|None, msg: dict|None)
    msg dict has keys: 'topic', 'payload'
    """

    def __init__(
        self,
        port: str,
        channel: int,
        pan_id: str,
        key: str,
        callback: Callable,
        auto_open: bool = True,
    ):
        """Initialize the WMS Stick.

        Args:
            port: Serial port path (e.g. '/dev/ttyUSB1')
            channel: WMS network channel (e.g. 17)
            pan_id: WMS network PAN ID (4-char hex, e.g. 'ABCD')
            key: WMS network key (32-char hex)
            callback: Callback function(error, msg)
            auto_open: If True, open port immediately on connect()
        """
        self.port_path = port
        self.channel = channel
        self.pan_id = pan_id.upper()
        self.key = key.upper()
        self.callback = callback
        self.auto_open = auto_open

        self.status = "created"
        self._serial: Optional[serial.Serial] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._queue_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._blinds: Dict[str, Blind] = {}  # keyed by snr_hex
        self._msg_queue: List[WmsMessage] = []
        self._queue_lock = threading.Lock()
        self._current_msg: Optional[WmsMessage] = None
        self._current_timeout_handle: Optional[threading.Timer] = None

        self._scanned_devices: Dict[str, dict] = {}
        self._scan_in_progress = False

        self._weather = {
            "snr": 0,
            "snr_hex": "000000",
            "temp": 0,
            "wind": 0,
            "lumen": 0,
            "rain": False,
        }

        # Position update polling
        self._pos_upd_interval: float = 0.0
        self._pos_upd_timer: Optional[threading.Timer] = None

        # Watch moving blinds interval
        self._watch_moving_interval: float = 0.0
        self._watch_moving_timer: Optional[threading.Timer] = None

        self._recv_buffer = ""

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def connect(self) -> None:
        """Open the serial port and initialize the WMS network.

        Starts background threads for reading and queue processing.
        Raises serial.SerialException on failure.
        """
        _LOGGER.info("WmsStick: Connecting to %s", self.port_path)
        self._serial = serial.Serial(
            port=self.port_path,
            baudrate=125000,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.1,
        )
        self.status = "init"

        self._stop_event.clear()

        # Start reader thread
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="wms-reader"
        )
        self._reader_thread.start()

        # Start queue processor thread
        self._queue_thread = threading.Thread(
            target=self._queue_loop, daemon=True, name="wms-queue"
        )
        self._queue_thread.start()

        # Initialize WMS network
        self._init_wms_network()

    def disconnect(self) -> None:
        """Close the serial port and stop background threads."""
        _LOGGER.info("WmsStick: Disconnecting from %s", self.port_path)
        self._stop_event.set()

        # Cancel timers
        if self._current_timeout_handle:
            self._current_timeout_handle.cancel()
            self._current_timeout_handle = None
        if self._pos_upd_timer:
            self._pos_upd_timer.cancel()
            self._pos_upd_timer = None
        if self._watch_moving_timer:
            self._watch_moving_timer.cancel()
            self._watch_moving_timer = None

        if self._serial and self._serial.is_open:
            self._serial.close()

        self.status = "created"

    def scan_devices(self, auto_assign_blinds: bool = False) -> None:
        """Scan the WMS network for devices.

        Results are delivered via callback with topic 'wms-vb-scanned-devices'.

        Args:
            auto_assign_blinds: If True, automatically add found blinds.
        """
        if self._scan_in_progress:
            _LOGGER.info("WmsStick: Scan already in progress")
            return

        self._scanned_devices = {}
        self._scan_in_progress = True

        def finish_scan(error, msg_sent, msg_rcv):
            """Called after the last scan request completes."""
            self._finish_scanned_devices(auto_assign_blinds)

        # Send 3 scan requests (some devices don't answer the first)
        self._enqueue(WmsMessage("scanRequest", 0, {"pan_id": self.pan_id}))
        self._enqueue(WmsMessage("scanRequest", 0, {"pan_id": self.pan_id}))
        msg = WmsMessage("scanRequest", 0, {"pan_id": self.pan_id})
        msg.on_end = finish_scan
        self._enqueue(msg)

    def blind_add(self, snr, name: str) -> Blind:
        """Add a blind to the stick's device list.

        Args:
            snr: Serial number (int or hex string)
            name: Logical name for the blind

        Returns:
            The created Blind object.
        """
        if isinstance(snr, int):
            snr_num = snr
            snr_hex = snr_num_to_hex(snr)
        else:
            snr_hex = str(snr).upper().zfill(6)
            snr_num = snr_hex_to_num(snr_hex)

        if snr_hex in self._blinds:
            _LOGGER.warning("WmsStick: Blind %s already added", snr_hex)
            return self._blinds[snr_hex]

        blind = Blind(snr=snr_num, snr_hex=snr_hex, name=name)
        self._blinds[snr_hex] = blind
        _LOGGER.info("WmsStick: Added blind %s (%s)", name, snr_hex)
        return blind

    def blind_remove(self, blind_id) -> int:
        """Remove a blind from the device list.

        Args:
            blind_id: snr (int), snr_hex (str), or name (str)

        Returns:
            Number of blinds removed.
        """
        blind = self._get_blind(blind_id)
        if blind and blind.snr_hex in self._blinds:
            del self._blinds[blind.snr_hex]
            _LOGGER.info("WmsStick: Removed blind %s", blind.snr_hex)
            return 1
        return 0

    def blind_get_position(self, blind_id=None, retry: int | None = None) -> None:
        """Request the current position of a blind.

        Args:
            blind_id: snr, snr_hex, or name. If None, queries all blinds.
            retry: Optional override for the message retry count. Background
                polls pass a low value (POS_POLL_RETRY); explicit user queries
                leave it None to use the CMD_SETTINGS default.
        """
        if blind_id is None:
            for blind in list(self._blinds.values()):
                self.blind_get_position(blind.snr_hex, retry=retry)
            return

        blind = self._get_blind(blind_id)
        if not blind:
            _LOGGER.warning(
                "WmsStick: blind_get_position: Cannot find blind '%s'", blind_id
            )
            return

        def on_complete(error, msg_sent, msg_rcv):
            # error is "" (empty string) on success, "timeout" on timeout
            if error != "timeout" and msg_rcv:
                p = msg_rcv.get("params", {})
                # The position poll cannot read the slat angle back while the
                # blind is raised (angle == None). Keep the last known angle in
                # that case instead of discarding it.
                ang = p.get("angle")
                if ang is None:
                    ang = blind.pos_current.ang
                new_pos = BlindPosition(
                    pos=p.get("position", blind.pos_current.pos),
                    ang=ang,
                    moving=p.get("moving", False),
                )
                self._update_blind_pos(blind, new_pos)

        msg = WmsMessage("blindGetPos", blind.snr, {})
        if retry is not None:
            msg.retry = retry
        msg.on_end = on_complete
        self._enqueue(msg)
        # Trigger queue processing (matching JS behavior)
        threading.Timer(DELAY_MSG_PROC, self._process_queue).start()

    def blind_set_position(
        self,
        blind_id,
        position: int,
        angle: int,
        on_complete: Optional[Callable] = None,
    ) -> None:
        """Move a blind to the specified position and angle.

        Args:
            blind_id: snr, snr_hex, or name
            position: 0-100 (0=open, 100=closed)
            angle: -100 to +100 (slat angle)
            on_complete: Optional callback(error, msg_sent, msg_rcv) called when
                         the motor acknowledges the command.
        """
        blind = self._get_blind(blind_id)
        if not blind:
            _LOGGER.warning(
                "WmsStick: blind_set_position: Cannot find blind '%s'", blind_id
            )
            return

        blind.pos_requested = BlindPosition(pos=position, ang=angle, moving=True)

        def _on_complete(error, msg_sent, msg_rcv):
            # error is "" (empty string) on success, "timeout" on timeout
            if error != "timeout":
                # Optimistically adopt the commanded angle. The position poll
                # cannot read the slat angle back (returns 0xFF while raised), so
                # without this the tilt state would never reflect what we just
                # commanded and the UI would appear "stuck".
                new_pos = BlindPosition(
                    pos=blind.pos_current.pos,
                    ang=blind.pos_requested.ang,
                    moving=True,
                )
                self._update_blind_pos(blind, new_pos)
            if on_complete:
                on_complete(error, msg_sent, msg_rcv)

        msg = WmsMessage("blindMoveToPos", blind.snr, {"pos": position, "ang": angle})
        msg.on_end = _on_complete
        self._enqueue(msg, priority=True)
        threading.Timer(DELAY_MSG_PROC, self._process_queue).start()

    def blind_stop(
        self,
        blind_id=None,
        get_pos_on_stop: bool = True,
        on_complete: Optional[Callable] = None,
    ) -> None:
        """Stop a blind's movement.

        Args:
            blind_id: snr, snr_hex, or name. If None, stops all blinds.
            get_pos_on_stop: If True, request position after stopping.
            on_complete: Optional callback(error, msg_sent, msg_rcv) called when
                         the motor acknowledges the stop command.
        """
        if blind_id is None:
            for blind in list(self._blinds.values()):
                self.blind_stop(blind.snr_hex, get_pos_on_stop=False)
            if get_pos_on_stop:
                for blind in list(self._blinds.values()):
                    self.blind_get_position(blind.snr_hex)
            return

        blind = self._get_blind(blind_id)
        if not blind:
            _LOGGER.warning("WmsStick: blind_stop: Cannot find blind '%s'", blind_id)
            return

        # Remove pending commands for this blind
        self._remove_queued_msgs(snr_hex=blind.snr_hex)

        msg = WmsMessage("blindStopMove", blind.snr, {})
        if on_complete:
            msg.on_end = on_complete
        self._enqueue(msg)
        # Kick the queue processor in case it is idle (e.g. after a delay_after period)
        threading.Timer(DELAY_MSG_PROC, self._process_queue).start()

        if get_pos_on_stop:
            self.blind_get_position(blind.snr_hex)

    def blind_wave(self, blind_id) -> None:
        """Send a wave (identify) request to a blind.

        Args:
            blind_id: snr, snr_hex, or name
        """
        blind = self._get_blind(blind_id)
        if not blind:
            _LOGGER.warning("WmsStick: blind_wave: Cannot find blind '%s'", blind_id)
            return

        msg = WmsMessage("waveRequest", blind.snr, {})
        self._enqueue(msg)

    def set_pos_upd_interval(self, interval_sec: float) -> None:
        """Set the interval for automatic position polling.

        Args:
            interval_sec: Polling interval in seconds. Set to 0 to disable.
                          Minimum effective value is 5 seconds.
        """
        if self._pos_upd_timer:
            self._pos_upd_timer.cancel()
            self._pos_upd_timer = None
        self._pos_upd_interval = interval_sec

        if interval_sec >= 5.0:
            _LOGGER.info("WmsStick: Position update interval: %.0f s", interval_sec)
            self._schedule_pos_upd()
        else:
            _LOGGER.info("WmsStick: Position update interval: disabled")

    def set_watch_moving_interval(self, interval_sec: float) -> None:
        """Set the interval for watching moving blinds.

        Args:
            interval_sec: Interval in seconds. Set to 0 to disable.
                          Minimum effective value is 0.1 seconds.
        """
        if self._watch_moving_timer:
            self._watch_moving_timer.cancel()
            self._watch_moving_timer = None
        self._watch_moving_interval = interval_sec

        if interval_sec >= 0.1:
            _LOGGER.info("WmsStick: Watch moving interval: %.1f s", interval_sec)
            self._schedule_watch_moving()

    def get_blinds(self) -> List[Blind]:
        """Return list of all registered blinds."""
        return list(self._blinds.values())

    def get_blind(self, blind_id) -> Optional[Blind]:
        """Get a blind by snr, snr_hex, or name."""
        return self._get_blind(blind_id)

    def get_weather(self) -> dict:
        """Return the last received weather broadcast."""
        return dict(self._weather)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _get_blind(self, blind_id) -> Optional[Blind]:
        """Find a blind by snr (int), snr_hex (str), or name (str)."""
        if blind_id is None:
            return None
        for blind in self._blinds.values():
            if blind.snr == blind_id:
                return blind
            if blind.snr_hex == str(blind_id).upper().zfill(6):
                return blind
            if blind.name == blind_id:
                return blind
        return None

    def _update_blind_pos(
        self, blind: Blind, new_pos: BlindPosition, force: bool = False
    ) -> None:
        """Update blind position and fire callback if changed (or forced).

        Args:
            blind: The blind to update.
            new_pos: The new position.
            force: If True, fire callback even if position is unchanged.
                   Used for the initial position read (pos_current starts at -1).
        """
        if force or not new_pos.equals(blind.pos_current):
            blind.pos_current = new_pos
            self.callback(
                None,
                {
                    "topic": "wms-vb-blind-position-update",
                    "payload": {
                        "snr": blind.snr,
                        "snr_hex": blind.snr_hex,
                        "name": blind.name,
                        "position": blind.pos_current.pos,
                        "angle": blind.pos_current.ang,
                        "moving": blind.pos_current.moving,
                    },
                },
            )

    def _enqueue(self, msg: WmsMessage, priority: bool = False) -> None:
        """Add a message to the command queue."""
        with self._queue_lock:
            if priority:
                self._msg_queue.insert(0, msg)
            else:
                self._msg_queue.append(msg)
        _LOGGER.debug("WmsStick: Enqueued %s snr=%s", msg.cmd, msg.snr)

    def _remove_queued_msgs(self, cmd: str = None, snr_hex: str = None) -> int:
        """Remove messages from the queue matching cmd and/or snr_hex."""
        removed = 0
        with self._queue_lock:
            i = 0
            while i < len(self._msg_queue):
                m = self._msg_queue[i]
                cmd_match = (cmd is None) or (m.cmd == cmd)
                snr_match = (
                    (snr_hex is None) or (m.snr == snr_hex) or (snr_hex == "000000")
                )
                if cmd_match and snr_match:
                    self._msg_queue.pop(i)
                    removed += 1
                else:
                    i += 1
        return removed

    def _has_queued_msg(self, cmd: str, snr_hex: str = None) -> bool:
        """Check if a message with the given cmd and optional snr_hex is queued.

        Args:
            cmd: Command type to check (e.g., "blindGetPos")
            snr_hex: Optional SNR hex to match. If None, matches any SNR.

        Returns:
            True if a matching message is found in the queue.
        """
        with self._queue_lock:
            for msg in self._msg_queue:
                if msg.cmd == cmd:
                    if snr_hex is None or msg.snr == snr_hex:
                        return True
        return False

    def _init_wms_network(self) -> None:
        """Send initialization sequence to the WMS stick."""
        _LOGGER.info("WmsStick: Initializing WMS network on %s", self.port_path)

        def on_init_complete(error, msg_sent, msg_rcv):
            if not error or error == "timeout":
                self.status = "ready"
                _LOGGER.info("WmsStick: Ready on %s", self.port_path)
                self.callback(
                    error if error else None,
                    {
                        "topic": "wms-vb-init-completion",
                        "payload": {"status": self.status},
                    },
                )
            else:
                _LOGGER.error("WmsStick: Init failed: %s", error)
                self.status = "error"

        self._enqueue(WmsMessage("stickGetName", 0, {}))
        self._enqueue(WmsMessage("stickGetVersion", 0, {}))
        self._enqueue(WmsMessage("stickSetKey", 0, {"key": self.key}))
        msg = WmsMessage(
            "stickSwitchChannel", 0, {"channel": self.channel, "pan_id": self.pan_id}
        )
        msg.on_end = on_init_complete
        self._enqueue(msg)

    def _finish_scanned_devices(self, auto_assign_blinds: bool) -> None:
        """Process scan results and fire callback."""
        devices = sorted(
            self._scanned_devices.values(),
            key=lambda d: d.get("device_type", "") + str(d.get("snr", 0)).zfill(10),
        )

        if auto_assign_blinds:
            self._blinds = {}
            for dev in devices:
                if dev.get("device_type", "") in BLIND_DEVICE_TYPES:
                    self.blind_add(
                        dev["snr"],
                        f"{dev['device_type_str']} {dev['snr']} ({dev['snr_hex']})",
                    )
                    _LOGGER.info(
                        "WmsStick: Auto-added blind %s (%s)",
                        dev["snr"],
                        dev["snr_hex"],
                    )

        self._scan_in_progress = False
        self.callback(
            None,
            {"topic": "wms-vb-scanned-devices", "payload": {"devices": devices}},
        )

    def _schedule_pos_upd(self) -> None:
        """Schedule the next position update poll."""
        if self._stop_event.is_set():
            return
        if self._pos_upd_interval >= 5.0:
            # Execute immediately (matching JS behavior: doPosUpdInterval())
            for blind in list(self._blinds.values()):
                # Dedup guard: skip blinds whose blindGetPos is still pending.
                # Without this, a slow/unreachable motor (whose query takes up to
                # retries * timeout) lets the queue grow without bound, because a
                # new poll is enqueued every interval regardless of backlog. This
                # mirrors _schedule_watch_moving / the JS doWatchMovingBlinds guard.
                if not self._has_queued_msg("blindGetPos", blind.snr_hex):
                    self.blind_get_position(blind.snr_hex, retry=POS_POLL_RETRY)
            # Then schedule the next execution
            self._pos_upd_timer = threading.Timer(
                self._pos_upd_interval, self._schedule_pos_upd
            )
            self._pos_upd_timer.daemon = True
            self._pos_upd_timer.start()

    def _schedule_watch_moving(self) -> None:
        """Schedule the next watch-moving check."""
        if self._stop_event.is_set():
            return
        if self._watch_moving_interval >= 0.1:
            # Execute immediately (matching JS behavior: doWatchMovingBlinds())
            for blind in list(self._blinds.values()):
                # Only query if moving AND not already queued (avoid duplicates)
                if blind.pos_current.moving and not self._has_queued_msg(
                    "blindGetPos", blind.snr_hex
                ):
                    self.blind_get_position(blind.snr_hex, retry=POS_POLL_RETRY)
            # Then schedule the next execution
            self._watch_moving_timer = threading.Timer(
                self._watch_moving_interval, self._schedule_watch_moving
            )
            self._watch_moving_timer.daemon = True
            self._watch_moving_timer.start()

    # -----------------------------------------------------------------------
    # Serial I/O
    # -----------------------------------------------------------------------

    def _send_frame(self, frame: str) -> None:
        """Write a frame string to the serial port."""
        if self._serial and self._serial.is_open:
            _LOGGER.debug("WMS-SND: %s", frame)
            self._serial.write(frame.encode("ascii"))
        else:
            _LOGGER.error("WmsStick: Cannot send, port not open")

    def _reconnect_serial(self) -> None:
        """Attempt to reconnect to the serial port with retries.

        Closes and reopens the serial port up to 5 times with 1 second delay
        between retries. Logs clearly when reconnecting and if all retries fail.
        """
        max_retries = 5
        retry_delay = 1.0

        for attempt in range(1, max_retries + 1):
            try:
                _LOGGER.info(
                    "WmsStick: Reconnection attempt %d/%d for %s",
                    attempt,
                    max_retries,
                    self.port_path,
                )

                # Close the port if it's open
                if self._serial and self._serial.is_open:
                    try:
                        self._serial.close()
                    except Exception as exc:  # pylint: disable=broad-except
                        _LOGGER.debug(
                            "WmsStick: Error closing port during reconnect: %s", exc
                        )

                # Wait before retrying
                if attempt < max_retries + 1:
                    time.sleep(retry_delay)

                # Reopen the port
                self._serial = serial.Serial(
                    port=self.port_path,
                    baudrate=125000,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0.1,
                )

                _LOGGER.info(
                    "WmsStick: Successfully reconnected to %s on attempt %d",
                    self.port_path,
                    attempt,
                )
                return  # Success

            except serial.SerialException as exc:
                _LOGGER.warning(
                    "WmsStick: Reconnection attempt %d/%d failed: %s",
                    attempt,
                    max_retries,
                    exc,
                )
                if attempt < max_retries:
                    # Continue to next retry
                    continue
                else:
                    # All retries exhausted
                    _LOGGER.error(
                        "WmsStick: Failed to reconnect to %s after %d attempts. Stopping reader thread.",
                        self.port_path,
                        max_retries,
                    )
                    self.status = "error"
                    break
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.exception(
                    "WmsStick: Unexpected error during reconnection attempt %d: %s",
                    attempt,
                    exc,
                )
                if attempt >= max_retries:
                    _LOGGER.error(
                        "WmsStick: Failed to reconnect to %s after %d attempts. Stopping reader thread.",
                        self.port_path,
                        max_retries,
                    )
                    self.status = "error"
                    break

    def _reader_loop(self) -> None:
        """Background thread: read from serial port and dispatch frames."""
        _LOGGER.debug("WmsStick: Reader thread started")
        while not self._stop_event.is_set():
            try:
                if self._serial and self._serial.is_open:
                    data = self._serial.read(256)
                    if data:
                        text = data.decode("ascii", errors="replace")
                        self._recv_buffer += text
                        # Split on '}' delimiter
                        while "}" in self._recv_buffer:
                            idx = self._recv_buffer.index("}")
                            frame = self._recv_buffer[: idx + 1]
                            self._recv_buffer = self._recv_buffer[idx + 1 :]
                            if frame.startswith("{"):
                                self._on_frame_received(frame)
            except serial.SerialException as exc:
                if not self._stop_event.is_set():
                    error_msg = str(exc)
                    # Check for the specific "device disconnected" error
                    if "readiness to read but returned no data" in error_msg:
                        _LOGGER.warning(
                            "WmsStick: Serial port access conflict detected, attempting reconnection: %s",
                            error_msg,
                        )
                        self._reconnect_serial()
                    else:
                        _LOGGER.error("WmsStick: Serial read error: %s", exc)
                        self.status = "error"
                        break
            except Exception as exc:  # pylint: disable=broad-except
                if not self._stop_event.is_set():
                    _LOGGER.exception("WmsStick: Unexpected reader error: %s", exc)
        _LOGGER.debug("WmsStick: Reader thread stopped")

    def _on_frame_received(self, frame: str) -> None:
        """Process a received frame."""
        _LOGGER.debug("WMS-RCV: %s", frame)
        msg = decode_frame(frame)
        self._handle_received_msg(msg)

    def _handle_received_msg(self, msg: dict) -> None:
        """Handle a decoded received message."""
        msg_type = msg["msg_type"]
        snr = msg["snr"]
        params = msg["params"]

        # Check if this is the expected response for the current command
        if (
            self._current_msg is not None
            and self._current_msg.stick_cmd.get("expect")
            and self._current_msg.stick_cmd["expect"]["msg_type"] == msg_type
            and (
                self._current_msg.stick_cmd["expect"]["snr"] is None
                or self._current_msg.stick_cmd["expect"]["snr"] == snr
            )
        ):
            _LOGGER.debug("WmsStick: Received expected response: %s", msg_type)
            # Cancel timeout
            if self._current_timeout_handle:
                self._current_timeout_handle.cancel()
                self._current_timeout_handle = None

            if self._current_msg.on_end:
                try:
                    self._current_msg.on_end("", self._current_msg, msg)
                except Exception as exc:  # pylint: disable=broad-except
                    _LOGGER.exception("WmsStick: on_end callback error: %s", exc)

            delay = self._current_msg.delay_after
            self._current_msg = None
            threading.Timer(delay + DELAY_MSG_PROC, self._process_queue).start()
            return

        # Unsolicited messages
        if msg_type == "weatherBroadcast":
            self._weather.update(
                {
                    "snr": msg["snr_num"],
                    "snr_hex": snr,
                    "temp": params.get("temp", 0),
                    "wind": params.get("wind", 0),
                    "lumen": params.get("lumen", 0),
                    "rain": params.get("rain", False),
                }
            )
            _LOGGER.debug("WmsStick: Weather: %s", self._weather)
            self.callback(
                None,
                {
                    "topic": "wms-vb-rcv-weather-broadcast",
                    "payload": {"weather": dict(self._weather)},
                },
            )

        elif msg_type == "scanResponse":
            dev_type = params.get("device_type", "00").upper()
            if snr not in self._scanned_devices:
                self._scanned_devices[snr] = {
                    "snr": msg["snr_num"],
                    "snr_hex": snr,
                    "device_type": dev_type,
                    "device_type_str": params.get("device_type_str", "<unknown>"),
                }
            _LOGGER.info(
                "WmsStick: Scanned device: %s Type %s %s",
                snr,
                dev_type,
                params.get("device_type_str", ""),
            )

        elif msg_type == "scanRequest":
            self.callback(
                None,
                {
                    "topic": "wms-vb-rcv-scan-request",
                    "payload": {"snr": msg["snr_num"]},
                },
            )
            resp = WmsMessage("scanResponse", snr, {"pan_id": self.pan_id})
            self._enqueue(resp)
            threading.Timer(DELAY_MSG_PROC, self._process_queue).start()

        elif msg_type == "switchChannelRequest":
            _LOGGER.debug("WmsStick: switchChannelRequest: %s", params)
            sw = WmsMessage(
                "stickSwitchChannel",
                snr,
                {
                    "channel": params.get("channel", self.channel),
                    "pan_id": params.get("pan_id", self.pan_id),
                },
            )
            self._enqueue(sw)
            threading.Timer(DELAY_MSG_PROC, self._process_queue).start()

        elif msg_type == "joinNetworkRequest":
            _LOGGER.info("WmsStick: joinNetworkRequest: %s", params)
            self.callback(
                None,
                {
                    "topic": "wms-vb-network-params",
                    "payload": {
                        "pan_id": params.get("pan_id"),
                        "network_key": params.get("network_key"),
                        "channel": params.get("channel"),
                    },
                },
            )
            ack = WmsMessage("ack", "000000", {})
            self._enqueue(ack)
            threading.Timer(DELAY_MSG_PROC, self._process_queue).start()

        elif msg_type == "waveRequest":
            self.callback(
                None,
                {
                    "topic": "wms-vb-rcv-wave-request",
                    "payload": {"snr": msg["snr_num"]},
                },
            )
            ack = WmsMessage("ack", "000000", {})
            self._enqueue(ack)
            threading.Timer(DELAY_MSG_PROC, self._process_queue).start()

        elif msg_type not in ("ack", "fwd"):
            _LOGGER.debug(
                "WmsStick: Unexpected message: %s snr=%s (waiting for: %s)",
                msg_type,
                snr,
                (
                    self._current_msg.stick_cmd["expect"]["msg_type"]
                    if self._current_msg
                    else "none"
                ),
            )

    # -----------------------------------------------------------------------
    # Queue processing
    # -----------------------------------------------------------------------

    def _queue_loop(self) -> None:
        """Background thread: kick off queue processing."""
        _LOGGER.debug("WmsStick: Queue thread started")
        # Wait for serial port to be ready
        time.sleep(0.1)
        self._process_queue()
        # The queue is driven by callbacks and timers after this point
        # Keep thread alive to handle any edge cases
        while not self._stop_event.is_set():
            time.sleep(1.0)
        _LOGGER.debug("WmsStick: Queue thread stopped")

    def _process_queue(self) -> None:
        """Process the next message in the queue if not busy."""
        if self._stop_event.is_set():
            return

        if self._current_msg is not None:
            _LOGGER.debug(
                "WmsStick: Queue busy, waiting for: %s",
                self._current_msg.stick_cmd.get("expect", {}).get("msg_type", "?"),
            )
            return

        with self._queue_lock:
            if not self._msg_queue:
                return
            self._current_msg = self._msg_queue.pop(0)

        self._current_msg.com_ts = time.time()
        frame = self._current_msg.stick_cmd.get("cmd", "")
        _LOGGER.debug(
            "WmsStick: Sending %s snr=%s frame=%s",
            self._current_msg.cmd,
            self._current_msg.snr,
            frame,
        )
        self._send_frame(frame)

        # Set timeout
        timeout = self._current_msg.timeout
        self._current_timeout_handle = threading.Timer(timeout, self._on_timeout)
        self._current_timeout_handle.daemon = True
        self._current_timeout_handle.start()

    def _on_timeout(self) -> None:
        """Handle command timeout."""
        if self._current_msg is None:
            return

        msg = self._current_msg
        _LOGGER.info(
            "WmsStick: Timeout for %s snr=%s (retry=%d)",
            msg.cmd,
            msg.snr,
            msg.retry,
        )

        if msg.retry > 0:
            # Retry: decrement counter and re-enqueue
            msg.retry -= 1
            self._current_msg = None
            self._current_timeout_handle = None
            self._enqueue(msg)
        else:
            # No more retries
            if msg.on_end:
                try:
                    msg.on_end("timeout", msg, None)
                except Exception as exc:  # pylint: disable=broad-except
                    _LOGGER.exception(
                        "WmsStick: on_end timeout callback error: %s", exc
                    )
            self._current_msg = None
            self._current_timeout_handle = None

        threading.Timer(DELAY_MSG_PROC, self._process_queue).start()
