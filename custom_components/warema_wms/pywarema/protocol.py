"""
WMS Protocol encoding/decoding.

Ported from JavaScript wms-vb-wmsutil.js.

Frame format: ASCII strings enclosed in '{' and '}'.
Delimiter character: '}'
Baud rate: 125000

SNR (Serial Number) encoding:
  - 6-digit hex string with byte reversal
  - e.g. integer 0x0A2469 → hex "0A2469" → wire format "69240A"

Position encoding:
  - 0-100% → stored as percent*2 in hex (0x00-0xC8)

Angle encoding:
  - -100 to +100% → stored as round(pct/100*75)+127 in hex (0x00-0xFE)
  - WMS_ANGLE constant = 75
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional, Union

# Device type table and platform routing live in device_types.py. Re-exported
# here so existing importers of protocol.DEVICE_TYPE_STRINGS keep working.
from .device_types import (
    BLIND_DEVICE_TYPES,
    COVER_DEVICE_TYPES,
    DEVICE_TYPE_STRINGS,
    LIGHT_DIMMER_DEVICE_TYPES,
    SUPPORTED_DEVICE_TYPES,
    TILT_DEVICE_TYPES,
    device_type_name,
    is_cover_device,
    is_light_device,
    is_supported_device,
    platform_for_device_type,
)

_LOGGER = logging.getLogger(__name__)

# Angle scaling constant (from JS: const wmsAngle = 75)
WMS_ANGLE = 75


# ---------------------------------------------------------------------------
# Product types (EProductType IDs as used by the WMS protocol)
# ---------------------------------------------------------------------------
#
# The "product" is the thing the motor is moving (a Raffstore vs an Awning vs a
# Roller Shutter) and is independent of the actuator *hardware* (Plug Receiver
# vs UP Aktuator vs Radio Motor). The integer ID lives in Block 37 addr 12
# alongside the isWithBlinds/isWithRuntime flags.

PRODUCT_BLOCK = 37
PRODUCT_ADDR = 12  # productType (UInt8)
PRODUCT_ADDR_CONTROL = 13  # controlType
PRODUCT_ADDR_BLINDS = 14  # isWithBlinds (0/1)
PRODUCT_ADDR_RUNTIME = 15  # isWithRuntime

PRODUCT_TYPE_NAMES: dict[int, str] = {
    0: "ExternalVenetianBlind",
    1: "InternalVenetianBlind",
    2: "RollerShutter",
    3: "Awning",
    4: "AwningOneValance",
    5: "AwningOneOrTwoWindsensors",
    6: "AwningOneValanceOneOrTwoWindsensors",
    7: "ConservatoryAwning",
    8: "FacadeAwning",
    9: "DroparmAwning",
    10: "VerticalAwning",
    11: "Markisolette",
    12: "PleatedBlindInside",
    13: "RollerBlindInside",
    14: "VerticalLouvreBlind",
    15: "Window",
    16: "LightSwitching",
    17: "LoadSwitching",
    18: "LightDimming",
    19: "LoadDimming",
    20: "PlugSocketSwitching",
    21: "Valance",
    22: "AwningTwoValances",
    23: "AwningTwoValancesOneOrTwoWindsensors",
    24: "SunSail",
    25: "PergolaAwning",
    26: "LedDimmable",
    27: "SlatRoofL60",
    28: "SlatRoofL70",
    29: "SlatRoofL70Tilting",
    30: "FloatingOutput",
    39: "ValanceRollerBlindSmart",
    255: "Unknown",
}

# Products that have slat-tilting hardware. Used as the authoritative answer
# when the per-device isWithBlinds flag in Block 37 is unreadable (motor asleep,
# old firmware, etc).
PRODUCT_TYPES_WITH_TILT: set[int] = {
    0,  # ExternalVenetianBlind
    1,  # InternalVenetianBlind
    14,  # VerticalLouvreBlind
    27,  # SlatRoofL60 (Lamellendach - tiltable louvres)
    28,  # SlatRoofL70 (Lamellendach - tiltable louvres)
    29,  # SlatRoofL70Tilting (Lamellendach - tiltable louvres)
}


def product_type_name(product_type: int | None) -> str:
    """Friendly name for a product type ID, or '<unknown>' for None/missing."""
    if product_type is None:
        return "<unknown>"
    return PRODUCT_TYPE_NAMES.get(product_type, f"ProductType{product_type}")


# ---------------------------------------------------------------------------
# SNR conversion helpers
# ---------------------------------------------------------------------------


def snr_num_to_hex(snr: int) -> str:
    """Convert integer SNR to 6-char hex string with byte reversal.

    Example: 0x0A2469 (664681) → "69240A"
    """
    hex_str = format(snr, "06X")
    # Byte reversal: bytes 0,1,2 → bytes 2,1,0
    return hex_str[4:6] + hex_str[2:4] + hex_str[0:2]


def snr_hex_to_num(hex_str: str) -> int:
    """Convert 6-char hex string (byte-reversed) to integer SNR.

    Example: "69240A" → 0x0A2469 (664681)
    """
    hex_str = hex_str.upper().zfill(6)
    # Reverse byte order back
    unreversed = hex_str[4:6] + hex_str[2:4] + hex_str[0:2]
    return int(unreversed, 16)


# ---------------------------------------------------------------------------
# Position / Angle conversion helpers
# ---------------------------------------------------------------------------


def pos_percent_to_hex(pos_percent: int) -> str:
    """Convert position percentage (0-100) to 2-char hex string.

    Stored as percent * 2 in hex.
    """
    clamped = max(0, min(100, pos_percent))
    return format(clamped * 2, "02X")


def pos_hex_to_percent(pos_hex: str) -> int:
    """Convert 2-char hex position to percentage (0-100)."""
    return round(int(pos_hex, 16) / 2)


def valance_percent_to_hex(valance_percent: int | None) -> str:
    """Convert a valance percentage to its 2-char hex settings byte.

    A valance uses the same encoding as a position (percent * 2). ``None``
    means "leave this channel alone" and maps to the 0xFF sentinel the
    protocol uses for an unset settings byte - the same value the position
    frame reports back for a channel the device does not have.
    """
    if valance_percent is None:
        return "FF"
    return pos_percent_to_hex(valance_percent)


def angle_percent_to_hex(ang_percent: int) -> str:
    """Convert angle percentage (-100 to +100) to 2-char hex string.

    Stored as clamp(round(pct/100*WMS_ANGLE), -75, 75) + 127.
    """
    raw = round(ang_percent / 100 * WMS_ANGLE)
    clamped = max(-WMS_ANGLE, min(WMS_ANGLE, raw))
    return format(clamped + 127, "02X")


def angle_hex_to_percent(ang_hex: str) -> int:
    """Convert 2-char hex angle to percentage (-100 to +100)."""
    return round((int(ang_hex, 16) - 127) / WMS_ANGLE * 100)


# ---------------------------------------------------------------------------
# Motor firmware parameters (Block 38 - persistent device-side settings)
# ---------------------------------------------------------------------------
#
# These are the persistent per-motor settings ("Position bei manueller
# Bedienung" etc.) that survive power cycles - they also apply when the handheld
# remote operates the motor. Block addresses and encodings:
#
#   manualOperation.settingDown.position  block 38 addr 301: byte = pct * 2
#   manualOperation.settingDown.slatAngle block 38 addr 302: byte = deg + 127
#   manualOperation.dwellTimeManualScene  block 38 addr 305: byte = minutes (unit TBC)
#   scene.scene0.position                 block 38 addr 307: byte = pct * 2
#   scene.scene0.slatAngle                block 38 addr 308: byte = deg + 127
#   common.isAbsent                       block 38 addr 1:   byte = 0/1
#
# 0xFF in any byte is the sentinel for "unset / not configured" - we map it
# to None.

MOTOR_PARAM_BLOCK = 38

# Products whose block 38 uses a DIFFERENT address layout than the one mapped
# out below. The addresses here describe the actuators driving blinds, shutters
# and awnings; on a slat roof the same region holds the louvre angle limits and
# the sensor assignments instead, and on a dimmer it holds the dimming settings.
#
# Reading - and above all writing - those addresses on such a device would hit
# unrelated parameters, so the motor parameter helpers refuse to touch them.
PRODUCT_TYPES_WITH_OTHER_PARAM_LAYOUT: set[int] = {
    16,  # LightSwitching
    17,  # LoadSwitching
    18,  # LightDimming
    19,  # LoadDimming
    20,  # PlugSocketSwitching
    26,  # LedDimmable
    27,  # SlatRoofL60 (Lamellendach)
    28,  # SlatRoofL70 (Lamellendach)
    29,  # SlatRoofL70Tilting (Lamellendach)
    30,  # FloatingOutput
}


def has_standard_param_layout(product_type: int | None) -> bool:
    """Return True when block 38 uses the address layout mapped out here.

    Unknown product types (None) are treated as standard: that is the state
    before the product info has been read, and it preserves the behaviour this
    integration had before the check existed.
    """
    return product_type not in PRODUCT_TYPES_WITH_OTHER_PARAM_LAYOUT


# Block 38 addresses for the persistent motor parameters.
ADDR_COMMON_IS_ABSENT = 1
ADDR_COMFORT_AUTO_ENABLED = 2  # common.isComfortAutoEnabled
ADDR_MANUAL_POSITION = 301
ADDR_MANUAL_SLAT_ANGLE = 302
ADDR_MANUAL_DUMMY = 303
ADDR_MANUAL_DWELL_TIME = 305
ADDR_COMFORT_POSITION = 307  # scene.scene0.position
ADDR_COMFORT_SLAT_ANGLE = 308  # scene.scene0.slatAngle
ADDR_ABSENT_POSITION = 439  # scene.absent.position
ADDR_ABSENT_SLAT_ANGLE = 440  # scene.absent.slatAngle
ADDR_RUN_TIME_UP = 463  # productSettings.runTimeUp (seconds)
ADDR_RUN_TIME_DOWN = 464  # productSettings.runTimeDown (seconds)
# addrs 465-468: padding / unknown
ADDR_CALIBRATION_UP = 469  # productSettings.calibrationTimeUp (seconds)
ADDR_CALIBRATION_DOWN = 470  # productSettings.calibrationTimeDown (seconds)
ADDR_TILTING_TIME = 471  # productSettings.tiltingTime (× 0.2 s)
ADDR_MIN_ANGLE = 472  # productSettings.minAngle (byte - 127 = degrees)
ADDR_MAX_ANGLE = 473  # productSettings.maxAngle
ADDR_TILTING_STEPS = 474  # productSettings.tiltingSteps
ADDR_MOTOR_ROTATION = 475  # productSettings.motorRotation (0=normal, 1=reversed)

# Block 81: firmware / hardware info (read-only)
SW_INFO_BLOCK = 81
SW_INFO_ADDR = 0
SW_INFO_SIZE = 32


def manual_position_to_byte(pct: int) -> int:
    """Encode position percentage (0..100) -> byte (0..200)."""
    return max(0, min(200, pct * 2))


def manual_position_from_byte(byte: int) -> Optional[int]:
    """Decode byte (0..200) -> percentage, or None for the 0xFF sentinel."""
    if byte == 0xFF:
        return None
    return byte // 2


def slat_angle_to_byte(degrees: int) -> int:
    """Encode slat angle (0..75 deg) -> byte (127..202)."""
    return max(127, min(202, degrees + 127))


def slat_angle_from_byte(byte: int) -> Optional[int]:
    """Decode byte (127..202) -> degrees, or None for the 0xFF sentinel."""
    if byte == 0xFF:
        return None
    return byte - 127


def product_angle_from_byte(byte: int) -> Optional[int]:
    """Decode signed-angle byte (0..254) -> degrees (-127..+127); 0xFF = None.

    Used for productSettings.minAngle / maxAngle where the range is -75..+75°
    and the neutral position (0°) is encoded as 127.
    """
    if byte == 0xFF:
        return None
    return byte - 127


def product_angle_to_byte(degrees: int) -> int:
    """Encode degrees (-127..+127) -> byte (0..254)."""
    return max(0, min(254, degrees + 127))


def tilting_time_from_byte(byte: int) -> Optional[float]:
    """Decode tiltingTime byte -> seconds (step 0.2 s); 0xFF = None."""
    if byte == 0xFF:
        return None
    return round(byte * 0.2, 1)


def tilting_time_to_byte(seconds: float) -> int:
    """Encode seconds -> tiltingTime byte (step 0.2 s, clipped to 0..254)."""
    return max(0, min(254, round(seconds / 0.2)))


@dataclass
class MotorParameters:
    """Firmware parameters of a Warema motor/actuator (Block 38).

    None for any field means either "not set on the device" (when read) or
    "leave unchanged" (when written).
    """

    # manualOperation.settingDown
    manual_position: Optional[int] = None  # %, 0..100
    manual_angle: Optional[int] = None  # degrees, 0..75
    manual_dwell_time: Optional[int] = None  # raw, 0..254

    # scene.scene0 = "Komfortposition" in the WMS Studio Pro UI
    comfort_position: Optional[int] = None  # %, 0..100
    comfort_angle: Optional[int] = None  # degrees, 0..75

    # common.isAbsent = "Status Abwesend" toggle
    is_absent: Optional[bool] = None

    # common.isComfortAutoEnabled = "Komfortfunktionen"
    comfort_auto_enabled: Optional[bool] = None

    # scene.absent = "Position/Winkel bei Status Abwesend"
    absent_position: Optional[int] = None  # %, 0..100
    absent_angle: Optional[int] = None  # degrees, 0..75

    # productSettings
    run_time_up: Optional[int] = None  # seconds, 0..254
    run_time_down: Optional[int] = None  # seconds, 0..254
    calibration_up: Optional[int] = None  # seconds, 0..254
    calibration_down: Optional[int] = None  # seconds, 0..254
    tilting_time: Optional[float] = None  # seconds (step 0.2), 0..50.8
    min_angle: Optional[int] = None  # degrees, -127..+127 (typically -75..0)
    max_angle: Optional[int] = None  # degrees, -127..+127 (typically 0..+75)
    tilting_steps: Optional[int] = None  # count, 0..254
    motor_rotation: Optional[bool] = None  # False=normal, True=reversed

    # Block 81: read-only firmware/hardware info (never written to device)
    software_version: Optional[str] = None
    device_type_name: Optional[str] = None


# ---------------------------------------------------------------------------
# Frame encoding
# ---------------------------------------------------------------------------


def encode_cmd(cmd: str, snr, params: dict) -> dict:
    """Encode a WMS command into a serial frame string.

    Returns a dict with:
      - 'cmd': the frame string to send
      - 'expect': {'msg_type': str, 'snr': str|None}

    Args:
        cmd: Command name string
        snr: Serial number (int or hex string). Use 0 or "000000" for broadcast.
        params: Command parameters dict
    """
    if isinstance(snr, int):
        snr_hex = snr_num_to_hex(snr)
    else:
        snr_hex = str(snr).upper().zfill(6)

    result = {"cmd": "", "expect": {"msg_type": "", "snr": None}}

    if cmd == "blindGetPos":
        result["expect"]["msg_type"] = "position"
        result["expect"]["snr"] = snr_hex
        result["cmd"] = "{R06" + snr_hex + "8010" + "01000005}"

    elif cmd == "blindMoveToPos":
        pos = params.get("pos", 0)
        ang = params.get("ang", 0)
        # Valance channels. The manual-command frame carries them in the two
        # settings bytes after the angle, encoded exactly like a position
        # (percent * 2). 0xFF is the protocol's "leave unchanged" sentinel and
        # stays the default, so a caller that does not mention a valance emits
        # a byte-for-byte identical frame to one that never knew about them.
        valance_1 = params.get("valance_1")
        valance_2 = params.get("valance_2")
        # ``ang=None`` masks the slat angle with the same 0xFF sentinel, so a
        # valance-only move leaves the slats where they are. Callers that pass
        # a real angle are unaffected.
        ang_hex = "FF" if ang is None else angle_percent_to_hex(ang)
        result["expect"]["msg_type"] = "blindMoveToPosResponse"
        result["expect"]["snr"] = snr_hex
        result["cmd"] = (
            "{R06"
            + snr_hex
            + "7070"
            + "03"
            + pos_percent_to_hex(pos)
            + ang_hex
            + valance_percent_to_hex(valance_1)
            + valance_percent_to_hex(valance_2)
            + "}"
        )

    elif cmd == "lightSetLevel":
        # Brightness for a dimming actuator. Same command and same encoding as
        # a motor position (percent * 2) in the first setting byte; the
        # remaining settings are 0xFF ("leave unchanged"), which on a dimmer
        # keeps its configured dimming speed and level limits untouched.
        level = params.get("level", 0)
        result["expect"]["msg_type"] = "blindMoveToPosResponse"
        result["expect"]["snr"] = snr_hex
        result["cmd"] = (
            "{R06" + snr_hex + "7070" + "03" + pos_percent_to_hex(level) + "FFFFFF}"
        )

    elif cmd == "blindStopMove":
        result["expect"]["msg_type"] = "blindMoveToPosResponse"
        result["expect"]["snr"] = snr_hex
        result["cmd"] = "{R06" + snr_hex + "7070" + "01" + "FF" + "FF" + "FFFF00}"

    elif cmd == "stickGetName":
        result["expect"]["msg_type"] = "stickName"
        result["cmd"] = "{G}"

    elif cmd == "stickGetVersion":
        result["expect"]["msg_type"] = "stickVersion"
        result["cmd"] = "{V}"

    elif cmd == "stickSetKey":
        key = params.get("key", "")
        result["expect"]["msg_type"] = "ack"
        result["cmd"] = "{K401" + key + "}"

    elif cmd == "stickSwitchChannel":
        channel = params.get("channel", 17)
        pan_id = params.get("pan_id", "0000")
        result["expect"]["msg_type"] = "ack"
        result["cmd"] = "{M%" + str(channel) + pan_id + "}"

    elif cmd == "scanRequest":
        pan_id = params.get("pan_id", "0000")
        result["expect"]["msg_type"] = ""
        result["cmd"] = "{R04FFFFFF7020" + pan_id + "02}"

    elif cmd == "scanResponse":
        pan_id = params.get("pan_id", "0000")
        result["expect"]["msg_type"] = "ackMsg"
        result["expect"]["snr"] = snr_hex
        result["cmd"] = "{R01" + snr_hex + "7021" + pan_id + "02}"

    elif cmd == "ack":
        result["expect"]["msg_type"] = ""
        result["cmd"] = "{a}"

    elif cmd == "ackMsg":
        result["expect"]["msg_type"] = "ack"
        result["cmd"] = "{R21" + snr_hex + "50AC}"

    elif cmd in ("waveRequest", "blindBeckonRequest"):
        result["expect"]["msg_type"] = "ackMsg"
        result["expect"]["snr"] = snr_hex
        result["cmd"] = "{R06" + snr_hex + "7050}"

    elif cmd == "mb8Read":
        # Generic MB8 block read - reads `size` bytes from <block, addr>.
        # Wire format: {R06<SNR>8010<BLOCK_hex2><ADDR_LE_hex4><SIZE_hex2>}
        block = int(params.get("block", 0))
        addr = int(params.get("addr", 0))
        size = int(params.get("size", 1))
        result["expect"]["msg_type"] = "mb8ReadResponse"
        result["expect"]["snr"] = snr_hex
        result["cmd"] = (
            "{R06"
            + snr_hex
            + "8010"
            + format(block & 0xFF, "02X")
            + format(addr & 0xFF, "02X")
            + format((addr >> 8) & 0xFF, "02X")
            + format(size & 0xFF, "02X")
            + "}"
        )

    elif cmd == "mb8Write":
        # Generic MB8 block write - writes `data` bytes to <block, addr>.
        # Wire format: {R06<SNR>8020<BLOCK_hex2><ADDR_LE_hex4><DATA>}
        # No length prefix - the data length is implicit from the frame size.
        block = int(params.get("block", 0))
        addr = int(params.get("addr", 0))
        data = params.get("data", b"")
        if isinstance(data, int):
            data = bytes([data])
        elif isinstance(data, list):
            data = bytes(data)
        elif isinstance(data, str):
            data = bytes.fromhex(data)
        elif not isinstance(data, (bytes, bytearray)):
            raise TypeError(
                f"mb8Write data must be bytes/int/list/hex-str, got {type(data)}"
            )
        result["expect"]["msg_type"] = "mb8WriteResponse"
        result["expect"]["snr"] = snr_hex
        result["cmd"] = (
            "{R06"
            + snr_hex
            + "8020"
            + format(block & 0xFF, "02X")
            + format(addr & 0xFF, "02X")
            + format((addr >> 8) & 0xFF, "02X")
            + bytes(data).hex().upper()
            + "}"
        )

    else:
        _LOGGER.error("encode_cmd: Unknown command '%s'", cmd)

    _LOGGER.debug("encode_cmd %s snr=%s → %s", cmd, snr_hex, result["cmd"])
    return result


# ---------------------------------------------------------------------------
# Frame decoding
# ---------------------------------------------------------------------------


def _wms_trim(data: str) -> str:
    """Strip leading '{' and trailing '}' from a WMS frame string."""
    pos = data.rfind("}")
    if pos >= 1:
        data = data[:pos]
    if data.startswith("{"):
        return data[1:].strip()
    return data.strip()


def decode_frame(raw: str) -> dict:
    """Decode a received WMS frame string into a message dict.

    Returns a dict with:
      - 'msg_type': str
      - 'snr': str (6-char hex, byte-reversed)
      - 'snr_num': int
      - 'params': dict (message-specific fields)
      - 'raw': str (original frame)
    """
    _LOGGER.debug("decode_frame: %s", raw)

    msg_type = "unknown"
    snr = "000000"
    params = {}

    if raw.startswith("{a}"):
        msg_type = "ack"

    elif raw.startswith("{f}"):
        msg_type = "fwd"

    elif raw.startswith("{g"):
        msg_type = "stickName"
        params["stick_name"] = _wms_trim(raw[2:])

    elif raw.startswith("{v"):
        msg_type = "stickVersion"
        params["stick_version"] = _wms_trim(raw[2:])

    elif raw.startswith("{r"):
        # {r<SNR6><TYPE4><PAYLOAD>}
        snr = raw[2:8].upper()
        rcv_type = raw[8:12].upper()
        payload = raw[12:]
        # Strip trailing '}'
        if payload.endswith("}"):
            payload = payload[:-1]

        if rcv_type == "8011":
            # Parameter get response
            param_type = payload[0:8].upper()
            if param_type in ("01000003", "01000005"):
                # Position response
                msg_type = "position"
                # 0xFF is the protocol's "not available" sentinel (see
                # wms-vb-wmsutil.js: "FF entspricht nicht vorhanden"). A freshly
                # added / unreferenced blind reports it; decode it as -1 (unknown)
                # instead of 0xFF/2 = 128%.
                _pos_hex = payload[8:10]
                params["position"] = (
                    -1 if _pos_hex.upper() == "FF" else pos_hex_to_percent(_pos_hex)
                )
                # The slat angle is only valid when the blind is lowered. While
                # raised, the actuator reports 0xFF (no defined angle). Treat that
                # as "unknown" (None) instead of fabricating a bogus ~171 value.
                _angle_hex = payload[10:12]
                params["angle"] = (
                    None
                    if _angle_hex.upper() == "FF"
                    else angle_hex_to_percent(_angle_hex)
                )
                params["valance_1"] = payload[12:14]
                params["valance_2"] = payload[14:16]
                params["moving"] = payload[16:18] != "00"
                # Raw state bytes, kept alongside the decoded values. Byte 0
                # and byte 1 are product-dependent (a position axis, a slat
                # angle or unused), so the raw values are what identifies how
                # a device reports its state. 0xFF means "not available".
                params["position_raw"] = int(_pos_hex, 16)
                params["angle_raw"] = int(_angle_hex, 16)
            elif param_type == "0C000006":
                # Auto modes & limits (wind/rain/sun/dusk threshold values + operating mode)
                msg_type = "autoSettings"
                # These are threshold values (0-255 scale, device-dependent interpretation)
                # Wind/Rain/Sun/Dusk: sensitivity thresholds
                # OP (operating mode): bit flags or mode selector
                params["wind"] = int(payload[12:14], 16)
                params["rain"] = int(payload[22:24], 16)
                params["sun"] = int(payload[24:26], 16)
                params["dusk"] = int(payload[26:28], 16)
                params["op"] = int(payload[28:30], 16)
            elif param_type == "26000046":
                msg_type = "clock"
                params["unknown"] = payload[20:]
            else:
                # Generic MB8 read response.
                # Wire format: <block_hex2><addr_LE_hex4><size_hex2><data_hex>
                # Used for arbitrary block/addr reads on top of the legacy
                # hardcoded position/clock/auto cases above.
                msg_type = "mb8ReadResponse"
                params["block"] = int(payload[0:2], 16)
                params["addr"] = int(payload[2:4], 16) | (int(payload[4:6], 16) << 8)
                size = int(payload[6:8], 16)
                params["size"] = size
                params["data"] = bytes.fromhex(payload[8 : 8 + size * 2])

        elif rcv_type == "8021":
            # MB8 write response - echoes block, addr and the written data.
            # Format: <block_hex2><addr_LE_hex4><data_hex>  (no length prefix)
            msg_type = "mb8WriteResponse"
            params["block"] = int(payload[0:2], 16)
            params["addr"] = int(payload[2:4], 16) | (int(payload[4:6], 16) << 8)
            params["data"] = bytes.fromhex(payload[6:])

        elif rcv_type == "7071":
            # Move to pos response
            msg_type = "blindMoveToPosResponse"
            params["unknown1"] = payload[0:10]
            params["prev_position"] = pos_hex_to_percent(payload[10:12])
            params["prev_angle"] = angle_hex_to_percent(payload[12:14])
            params["prev_valance_1"] = payload[14:16]
            params["prev_valance_2"] = payload[16:18]
            params["unknown2"] = payload[18:26]

        elif rcv_type == "7080":
            # Weather broadcast
            msg_type = "weatherBroadcast"
            params["unknown_1"] = payload[0:2]
            wind_raw = int(payload[2:4], 16)
            params["wind"] = wind_raw
            lumen_hi = payload[4:6]
            lumen_lo = int(payload[12:14], 16)
            if lumen_hi == "00":
                params["lumen"] = lumen_lo * 2
            else:
                params["lumen"] = int(lumen_hi, 16) * lumen_lo * 2
            params["unknown_2"] = payload[6:12]
            params["unknown_3"] = payload[14:16]
            params["rain"] = payload[16:18] == "C8"
            params["temp"] = int(payload[18:20], 16) / 2 - 35
            params["unknown_4"] = payload[20:]

        elif rcv_type == "8020":
            param_type = payload[0:8].upper()
            if param_type == "0B080009":
                msg_type = "clock"
                params["year"] = int(payload[8:10], 16)
                params["month"] = int(payload[10:12], 16)
                params["day"] = int(payload[12:14], 16)
                params["hour"] = int(payload[14:16], 16)
                params["minute"] = int(payload[16:18], 16)
                params["second"] = int(payload[18:20], 16)
                params["day_of_week"] = int(payload[20:22], 16)
                params["unknown"] = payload[22:]

        elif rcv_type == "5018":
            # Join network request (used to capture network params)
            msg_type = "joinNetworkRequest"
            params["pan_id"] = payload[0:4]
            # Network key: 32 hex chars, byte-reversed in pairs
            key_raw = payload[4:36]
            key_bytes = [key_raw[i : i + 2] for i in range(0, 32, 2)]
            params["network_key"] = "".join(reversed(key_bytes))
            params["unknown"] = payload[36:38]
            params["channel"] = int(payload[38:40], 16)

        elif rcv_type == "5060":
            # Switch channel request
            msg_type = "switchChannelRequest"
            params["pan_id"] = payload[0:4]
            params["device_type"] = payload[4:6]
            params["channel"] = int(payload[6:8], 16)

        elif rcv_type == "50AC":
            # Ack message
            msg_type = "ackMsg"
            params["unknown"] = payload[0:4]

        elif rcv_type == "7020":
            # Scan request from device
            msg_type = "scanRequest"
            params["pan_id"] = payload[0:4]
            params["device_type"] = payload[4:6]

        elif rcv_type == "7021":
            # Scan response from device
            msg_type = "scanResponse"
            params["pan_id"] = payload[0:4]
            params["device_type"] = payload[4:6].upper()
            params["device_type_str"] = DEVICE_TYPE_STRINGS.get(
                params["device_type"], "<unknown>"
            )
            params["unknown"] = payload[6:]

        elif rcv_type == "7050":
            # Wave request
            msg_type = "waveRequest"

        elif rcv_type == "7070":
            # Blind move to pos (received from another controller)
            msg_type = "blindMoveToPos"
            params["unknown"] = payload[0:2]
            params["position"] = pos_hex_to_percent(payload[2:4])
            params["angle"] = angle_hex_to_percent(payload[4:6])
            params["valance_1"] = payload[6:8]
            params["valance_2"] = payload[8:10]

        elif rcv_type == "8010":
            # Parameter get request
            msg_type = "parameterGetRequest"
            params["parameter"] = payload

        else:
            _LOGGER.debug(
                "decode_frame: Unknown rcv_type '%s' in frame: %s", rcv_type, raw
            )

    else:
        _LOGGER.debug("decode_frame: Unrecognized frame: %s", raw)

    snr_num = snr_hex_to_num(snr)

    result = {
        "msg_type": msg_type,
        "snr": snr,
        "snr_num": snr_num,
        "params": params,
        "raw": raw,
    }
    _LOGGER.debug("decode_frame result: %s", result)
    return result
