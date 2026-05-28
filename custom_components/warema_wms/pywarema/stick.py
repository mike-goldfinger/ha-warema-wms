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
    ADDR_COMFORT_POSITION,
    ADDR_COMFORT_SLAT_ANGLE,
    ADDR_COMMON_IS_ABSENT,
    ADDR_MANUAL_DWELL_TIME,
    ADDR_MANUAL_POSITION,
    ADDR_MANUAL_SLAT_ANGLE,
    BLIND_DEVICE_TYPES,
    MOTOR_PARAM_BLOCK,
    MotorParameters,
    decode_frame,
    encode_cmd,
    manual_position_from_byte,
    manual_position_to_byte,
    slat_angle_from_byte,
    slat_angle_to_byte,
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

    # ---- Motor firmware parameter R/W ------------------------------------
    #
    # The methods below read and write the persistent firmware parameters
    # that WMS Studio Pro programs into the motor itself (manualOperation,
    # comfortPosition, isAbsent). These survive power cycles and apply when
    # the handheld remote operates the motor.
    #
    # Wire-format reference: see re/PROTOCOL.md (sections 8-10) - verified
    # against a live WMS Studio Pro trace.

    def mb8_read(
        self,
        blind_id,
        block: int,
        addr: int,
        size: int,
        on_complete: Optional[Callable] = None,
    ) -> None:
        """Read raw bytes from a parameter block on a device.

        Sends a generic MB8 read request (opcode 0x8010) and delivers the
        response to ``on_complete``. The response's ``params['data']`` is the
        raw ``bytes`` returned by the device.

        Args:
            blind_id: Device identifier (snr int, snr_hex str, or name).
            block: Block number (0-255), e.g. 38 for the parameter block.
            addr: Block address (0-65535).
            size: Number of bytes to read (1-255).
            on_complete: Optional callback(error, msg_sent, msg_rcv).
        """
        blind = self._get_blind(blind_id)
        if not blind:
            _LOGGER.warning("WmsStick: mb8_read: Cannot find blind '%s'", blind_id)
            if on_complete:
                on_complete("blind-not-found", None, None)
            return

        msg = WmsMessage(
            "mb8Read", blind.snr, {"block": block, "addr": addr, "size": size}
        )
        if on_complete:
            msg.on_end = on_complete
        self._enqueue(msg)
        threading.Timer(DELAY_MSG_PROC, self._process_queue).start()

    def mb8_write(
        self,
        blind_id,
        block: int,
        addr: int,
        data,
        on_complete: Optional[Callable] = None,
    ) -> None:
        """Write raw bytes to a parameter block on a device.

        Sends a generic MB8 write request (opcode 0x8020). The device echoes
        the write back via opcode 0x8021 - the echo is delivered through
        ``on_complete``.

        Args:
            blind_id: Device identifier.
            block: Block number.
            addr: Block address.
            data: Bytes to write - accepts bytes, list[int], int (single byte),
                  or hex string.
            on_complete: Optional callback(error, msg_sent, msg_rcv).
        """
        blind = self._get_blind(blind_id)
        if not blind:
            _LOGGER.warning("WmsStick: mb8_write: Cannot find blind '%s'", blind_id)
            if on_complete:
                on_complete("blind-not-found", None, None)
            return

        msg = WmsMessage(
            "mb8Write", blind.snr, {"block": block, "addr": addr, "data": data}
        )
        if on_complete:
            msg.on_end = on_complete
        self._enqueue(msg)
        threading.Timer(DELAY_MSG_PROC, self._process_queue).start()

    def read_motor_parameters(
        self, blind_id, timeout: float = 5.0
    ) -> Optional[MotorParameters]:
        """Read the persistent firmware parameters of an actuator (block 38).

        Synchronously reads manualOperation.settingDown (addrs 301..305),
        scene.scene0 (=Komfortposition, addrs 307..308) and common.isAbsent
        (addr 1) in three MB8 reads.

        Returns:
            A populated ``MotorParameters`` on success, or ``None`` if any
            read timed out / failed. Individual fields can still be ``None``
            when the device returned the 0xFF "unset" sentinel.
        """
        blind = self._get_blind(blind_id)
        if not blind:
            _LOGGER.warning(
                "WmsStick: read_motor_parameters: Cannot find blind '%s'", blind_id
            )
            return None

        params = MotorParameters()
        done = threading.Event()
        remaining = [3]
        failed = [False]

        def make_cb(setter):
            def cb(error, msg_sent, msg_rcv):
                if error == "timeout" or msg_rcv is None:
                    failed[0] = True
                else:
                    try:
                        setter(msg_rcv["params"].get("data", b""))
                    except Exception as exc:  # pylint: disable=broad-except
                        _LOGGER.exception(
                            "read_motor_parameters: decode error: %s", exc
                        )
                        failed[0] = True
                remaining[0] -= 1
                if remaining[0] <= 0:
                    done.set()

            return cb

        def set_manual(data: bytes) -> None:
            if len(data) >= 5:
                params.manual_position = manual_position_from_byte(data[0])
                params.manual_angle = slat_angle_from_byte(data[1])
                # data[2] = dummy (always 0xFF), data[3] reserved
                params.manual_dwell_time = (
                    None if data[4] == 0xFF else int(data[4])
                )

        def set_comfort(data: bytes) -> None:
            if len(data) >= 2:
                params.comfort_position = manual_position_from_byte(data[0])
                params.comfort_angle = slat_angle_from_byte(data[1])

        def set_absent(data: bytes) -> None:
            if len(data) >= 1:
                params.is_absent = None if data[0] == 0xFF else bool(data[0])

        self.mb8_read(
            blind.snr_hex,
            block=MOTOR_PARAM_BLOCK,
            addr=ADDR_MANUAL_POSITION,
            size=5,
            on_complete=make_cb(set_manual),
        )
        self.mb8_read(
            blind.snr_hex,
            block=MOTOR_PARAM_BLOCK,
            addr=ADDR_COMFORT_POSITION,
            size=2,
            on_complete=make_cb(set_comfort),
        )
        self.mb8_read(
            blind.snr_hex,
            block=MOTOR_PARAM_BLOCK,
            addr=ADDR_COMMON_IS_ABSENT,
            size=1,
            on_complete=make_cb(set_absent),
        )

        if not done.wait(timeout):
            _LOGGER.warning(
                "WmsStick: read_motor_parameters: timeout for %s", blind.snr_hex
            )
            return None
        if failed[0]:
            _LOGGER.warning(
                "WmsStick: read_motor_parameters: one or more reads failed for %s",
                blind.snr_hex,
            )
            return None
        return params

    # Transfer-block protocol constants (verified against WMS Studio Pro live
    # trace, see re/PROTOCOL.md section 9). The motor only accepts persistent
    # writes to "isPartOfParamProcess" parameters via this 2-phase flow:
    # write 8 data chunks + control/trailer bytes to block 8, then commit at
    # 0x01F7 to trigger an atomic copy block 8 -> block 38.
    _TB_CHUNK_STARTS = (0x0007, 0x004A, 0x008D, 0x00D0, 0x0113, 0x0156, 0x0199, 0x01DC)
    _TB_DATA_BYTES_PER_FULL_CHUNK = 67  # 7 full chunks of 67 + 1 chunk of 27 = 496 bytes
    _TB_DATA_BYTES_LAST_CHUNK = 27
    _TB_BLOCK_38_OFFSET = 8  # block_8_addr = block_38_addr + 8 (~90% match in trace)
    _TB_BLOCK_38_SIZE = 496  # block 38 covers addrs 0..495

    # Control writes that frame the chunk transfer. Order matters - this is
    # exactly what WMS Studio Pro sends after the chunks (see trace
    # 2026-05-28.txt, time window 09:13:01.5 - 09:13:01.9).
    _TB_HEADER_ADDR = 0x0000
    _TB_HEADER_DATA = bytes.fromhex("03010103")
    _TB_TRAILER1_ADDR = 0x0007  # overwrites first 5 bytes of chunk 0
    _TB_TRAILER1_DATA = bytes.fromhex("0401FF00FF")
    _TB_TRAILER2_ADDR = 0x0001  # overwrites addrs 0x0001-0x0007 (incl. last byte of trailer1)
    _TB_TRAILER2_DATA = bytes.fromhex("06010400000200")
    _TB_COMMIT_ADDR = 0x01F7
    _TB_COMMIT_DATA = bytes.fromhex("0101")

    def _mb8_read_sync(
        self, snr_hex, block: int, addr: int, size: int, timeout: float = 3.0
    ) -> Optional[bytes]:
        """Synchronous MB8 block read - returns ``bytes`` on success, None on timeout."""
        done = threading.Event()
        holder: dict = {"data": None, "error": None}

        def cb(error, msg_sent, msg_rcv):
            if error == "timeout" or msg_rcv is None:
                holder["error"] = error or "no-response"
            else:
                holder["data"] = msg_rcv["params"].get("data", b"")
            done.set()

        self.mb8_read(snr_hex, block=block, addr=addr, size=size, on_complete=cb)
        if not done.wait(timeout):
            _LOGGER.warning(
                "mb8_read_sync: timeout block=%d addr=%d size=%d", block, addr, size
            )
            return None
        return holder["data"]

    def _mb8_write_sync(
        self, snr_hex, block: int, addr: int, data: bytes, timeout: float = 3.0
    ) -> bool:
        """Synchronous MB8 write - returns True on ack, False on timeout."""
        done = threading.Event()
        holder: dict = {"ack": False, "echo": None}

        def cb(error, msg_sent, msg_rcv):
            if error != "timeout" and msg_rcv is not None:
                holder["ack"] = True
                holder["echo"] = msg_rcv["params"].get("data")
            done.set()

        self.mb8_write(snr_hex, block=block, addr=addr, data=data, on_complete=cb)
        if not done.wait(timeout):
            _LOGGER.warning(
                "mb8_write_sync: timeout block=%d addr=%d len=%d",
                block,
                addr,
                len(data),
            )
            return False
        return holder["ack"]

    def _read_full_block_38(self, snr_hex, timeout: float = 5.0) -> Optional[bytes]:
        """Read all 496 bytes of block 38 via a sequence of MB8 reads.

        Uses 32-byte read chunks - that matches the largest read size observed
        in the WMS Studio Pro live trace (`0x20`). Larger reads in our earlier
        attempts failed on the user's device firmware, so we stay conservative.

        Each read is retried up to 3 times on timeout. The full 496-byte read
        therefore takes ~5s under good conditions, up to ~30s if many retries.

        Returns the concatenated buffer or None if any read failed permanently.
        """
        chunk_size = 32
        max_retries_per_chunk = 3
        parts: list[bytes] = []
        for offset in range(0, self._TB_BLOCK_38_SIZE, chunk_size):
            n = min(chunk_size, self._TB_BLOCK_38_SIZE - offset)
            data = None
            for attempt in range(max_retries_per_chunk):
                data = self._mb8_read_sync(
                    snr_hex,
                    block=MOTOR_PARAM_BLOCK,
                    addr=offset,
                    size=n,
                    timeout=timeout,
                )
                if data is not None and len(data) >= n:
                    break
                _LOGGER.info(
                    "_read_full_block_38: retry %d/%d at offset %d (got %s bytes)",
                    attempt + 1,
                    max_retries_per_chunk,
                    offset,
                    None if data is None else len(data),
                )
            if data is None or len(data) < n:
                _LOGGER.warning(
                    "_read_full_block_38: GIVING UP at offset %d after %d attempts",
                    offset,
                    max_retries_per_chunk,
                )
                return None
            parts.append(data[:n])
        full = b"".join(parts)
        _LOGGER.info(
            "_read_full_block_38: read %d bytes from %s (first 16: %s)",
            len(full),
            snr_hex,
            full[:16].hex(),
        )
        return full

    def _write_via_transfer_block(
        self,
        snr_hex,
        block_38_data: bytes,
        per_write_timeout: float = 3.0,
    ) -> bool:
        """Write the full block-38 snapshot via the 2-phase transfer-block protocol.

        Sends 8 data chunks (block 8 addrs 0x0007/0x004A/0x008D/0x00D0/0x0113/
        0x0156/0x0199/0x01DC), 1 header byte, 2 trailer writes, then the commit
        byte at 0x01F7. See ``re/PROTOCOL.md`` section 9 for the byte-level
        protocol derived from the WMS Studio Pro trace.

        Returns True if every wire write was acked, False otherwise. Does NOT
        verify that the commit actually persisted - the caller should re-read
        block 38 to confirm.
        """
        if len(block_38_data) != self._TB_BLOCK_38_SIZE:
            raise ValueError(
                f"block_38_data must be {self._TB_BLOCK_38_SIZE} bytes, got {len(block_38_data)}"
            )

        # Slice the 496-byte payload into 7 full chunks + 1 short chunk.
        # Each chunk's WIRE payload is: 1 length-prefix byte (0x43 or 0x1B)
        # followed by the chunk's data bytes from block_38_data. The next
        # chunk's address overlaps by 1 byte with the previous chunk's last
        # data byte - that's how WMS Studio Pro does it (motor firmware
        # handles the overlap as a control marker).
        src = 0
        for i, chunk_addr in enumerate(self._TB_CHUNK_STARTS):
            if i < len(self._TB_CHUNK_STARTS) - 1:
                data_len = self._TB_DATA_BYTES_PER_FULL_CHUNK  # 67
            else:
                data_len = self._TB_DATA_BYTES_LAST_CHUNK  # 27
            wire_payload = bytes([data_len]) + block_38_data[src : src + data_len]
            ok = self._mb8_write_sync(
                snr_hex,
                block=8,
                addr=chunk_addr,
                data=wire_payload,
                timeout=per_write_timeout,
            )
            if not ok:
                _LOGGER.warning(
                    "transfer_block: chunk %d write to block8/addr=0x%04X failed", i, chunk_addr
                )
                return False
            src += data_len
        assert src == self._TB_BLOCK_38_SIZE, f"chunk slicing covered {src} bytes, expected 496"

        # Header at 0x0000 (init transfer)
        if not self._mb8_write_sync(
            snr_hex, block=8, addr=self._TB_HEADER_ADDR, data=self._TB_HEADER_DATA, timeout=per_write_timeout
        ):
            _LOGGER.warning("transfer_block: header write failed")
            return False
        # Read-back of addr 0x0000 (status check observed in trace, ignored value)
        self._mb8_read_sync(snr_hex, block=8, addr=self._TB_HEADER_ADDR, size=1, timeout=per_write_timeout)

        # Trailer 1 at 0x0007 (5 bytes - overwrites first bytes of chunk 0)
        if not self._mb8_write_sync(
            snr_hex, block=8, addr=self._TB_TRAILER1_ADDR, data=self._TB_TRAILER1_DATA, timeout=per_write_timeout
        ):
            _LOGGER.warning("transfer_block: trailer1 write failed")
            return False
        # Trailer 2 at 0x0001 (7 bytes - overwrites trailer1's last byte too)
        if not self._mb8_write_sync(
            snr_hex, block=8, addr=self._TB_TRAILER2_ADDR, data=self._TB_TRAILER2_DATA, timeout=per_write_timeout
        ):
            _LOGGER.warning("transfer_block: trailer2 write failed")
            return False

        # Commit at 0x01F7 (triggers atomic copy block 8 -> block 38)
        if not self._mb8_write_sync(
            snr_hex, block=8, addr=self._TB_COMMIT_ADDR, data=self._TB_COMMIT_DATA, timeout=per_write_timeout
        ):
            _LOGGER.warning("transfer_block: commit write failed")
            return False
        # Read-back of commit status (observed in trace).
        self._mb8_read_sync(snr_hex, block=8, addr=self._TB_COMMIT_ADDR, size=1, timeout=per_write_timeout)

        _LOGGER.info("transfer_block: full flow completed for %s", snr_hex)
        return True

    def write_motor_parameters(
        self,
        blind_id,
        params: MotorParameters,
        timeout: float = 30.0,
    ) -> bool:
        """Write changed firmware parameters via the transfer-block protocol.

        The flow is:

          1. Read the full block-38 snapshot (496 bytes) from the device.
          2. Patch the bytes for the user-requested changes.
          3. Send 8 chunks + header + trailers + commit to block 8.
          4. Read back block 38 and verify the patched bytes match expectations.

        Only fields where the corresponding attribute is not ``None`` are
        patched - everything else stays as-is on the device. The total
        operation typically takes 5-10 seconds.

        Returns True on a verified successful write, False otherwise.
        """
        blind = self._get_blind(blind_id)
        if not blind:
            _LOGGER.warning(
                "WmsStick: write_motor_parameters: Cannot find blind '%s'", blind_id
            )
            return False

        # 1. Read current block-38 snapshot.
        _LOGGER.info("write_motor_parameters: reading current block 38 from %s", blind.snr_hex)
        current = self._read_full_block_38(blind.snr_hex, timeout=timeout)
        if current is None:
            _LOGGER.warning(
                "write_motor_parameters: could not read block 38 from %s (motor asleep?)",
                blind.snr_hex,
            )
            return False

        # 2. Build the patch.
        patched = bytearray(current)
        patches: list[tuple[str, int, int]] = []
        if params.manual_position is not None:
            patches.append(
                ("manual_position", ADDR_MANUAL_POSITION, manual_position_to_byte(params.manual_position))
            )
        if params.manual_angle is not None:
            patches.append(
                ("manual_angle", ADDR_MANUAL_SLAT_ANGLE, slat_angle_to_byte(params.manual_angle))
            )
        if params.manual_dwell_time is not None:
            patches.append(
                ("manual_dwell_time", ADDR_MANUAL_DWELL_TIME, int(params.manual_dwell_time) & 0xFF)
            )
        if params.comfort_position is not None:
            patches.append(
                ("comfort_position", ADDR_COMFORT_POSITION, manual_position_to_byte(params.comfort_position))
            )
        if params.comfort_angle is not None:
            patches.append(
                ("comfort_angle", ADDR_COMFORT_SLAT_ANGLE, slat_angle_to_byte(params.comfort_angle))
            )
        if params.is_absent is not None:
            patches.append(
                ("is_absent", ADDR_COMMON_IS_ABSENT, 1 if params.is_absent else 0)
            )

        if not patches:
            _LOGGER.info(
                "write_motor_parameters: nothing requested for %s", blind.snr_hex
            )
            return True

        for label, addr, new_byte in patches:
            old_byte = patched[addr]
            patched[addr] = new_byte
            _LOGGER.info(
                "  patch %s @addr=%d: 0x%02X -> 0x%02X",
                label,
                addr,
                old_byte,
                new_byte,
            )

        # 3. Write via transfer block protocol.
        if not self._write_via_transfer_block(blind.snr_hex, bytes(patched)):
            _LOGGER.warning(
                "write_motor_parameters: transfer-block write failed for %s",
                blind.snr_hex,
            )
            return False

        # 4. Verify by re-reading block 38 and checking the patched bytes.
        verify = self._read_full_block_38(blind.snr_hex, timeout=timeout)
        if verify is None:
            _LOGGER.warning(
                "write_motor_parameters: post-commit read-back failed - cannot verify"
            )
            return False
        mismatches = []
        for label, addr, expected in patches:
            got = verify[addr]
            if got != expected:
                mismatches.append((label, addr, expected, got))
        if mismatches:
            _LOGGER.warning(
                "write_motor_parameters: verify FAILED for %s: %s",
                blind.snr_hex,
                ", ".join(
                    f"{lbl}@{a}: expected 0x{e:02X} got 0x{g:02X}"
                    for lbl, a, e, g in mismatches
                ),
            )
            return False

        _LOGGER.info(
            "write_motor_parameters: verified OK for %s (%d field(s))",
            blind.snr_hex,
            len(patches),
        )
        return True

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

        # Keep a local reference so the rest of this function is race-safe.
        # The reader thread can set self._current_msg = None at any time after
        # _send_frame returns (a fast device response arrives within
        # microseconds), so we must NOT read self._current_msg.timeout etc.
        # after sending.
        msg = self._current_msg
        msg.com_ts = time.time()
        frame = msg.stick_cmd.get("cmd", "")
        _LOGGER.debug(
            "WmsStick: Sending %s snr=%s frame=%s",
            msg.cmd,
            msg.snr,
            frame,
        )
        timeout = msg.timeout

        self._send_frame(frame)

        # Set timeout - guard the assignment in case the response already
        # consumed the message and cleared the slot before we get here.
        if self._current_msg is msg:
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
