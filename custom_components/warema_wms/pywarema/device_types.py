"""
WMS device types and platform routing.

Single source of truth for the device-type table and for the mapping from a
device type to the Home Assistant platform that represents it.  Lives in
``pywarema`` (not in ``const.py``) so the protocol package stays importable on
its own; ``const.py`` re-exports the names for the HA-side modules.

Device types are the 2-char uppercase hex strings carried in the scan response
(``7021``) and in ``DEVICE_TYPE_STRINGS`` below.

Actuator classes:
  - Covers   : plug receivers, in-wall actuators and radio motors that drive
               blinds, shutters, awnings and slat roofs.
  - Dimmers  : stand-alone dimming actuators.  These are separate devices with
               their own serial number - a light is never a sub-channel of a
               motor - and they answer the same broadcast scan as any motor.
"""

# ---------------------------------------------------------------------------
# Device type table
# ---------------------------------------------------------------------------

DEVICE_TYPE_STRINGS: dict[str, str] = {
    "02": "Stick/software",
    "06": "Weather station",
    "07": "Remote control (+)",
    "20": "Actuator UP",
    "21": "Plug receiver",
    "25": "Radio motor",
    "26": "Dimmer",
    "28": "Dimmer (smart)",
    "2A": "Radio motor (Lamellendach L60/L70)",
    "2E": "Actuator 230V UP",
    "30": "Plug receiver (new generation)",
    "31": "Dimmer 0-10 V",
    "63": "Web control",
}

# Device types that drive a cover (blind / shutter / awning / slat roof).
# 2A is the motor used by the slat-roof (Lamellendach) products SlatRoofL60,
# SlatRoofL70 and SlatRoofL70Tilting.
# 30 is a newer plug receiver generation (WMS Studio still calls it
# "Zwischenstecker"); it answers the scan like a 21 and drives an awning in
# the field (issue #18). Treated like 21 for cover control.
COVER_DEVICE_TYPES: set[str] = {"20", "21", "25", "2A", "2E", "30"}

# Dimming actuators, exposed as light entities.
#
# 28 is confirmed against real hardware: brightness is carried by the same
# command and the same encoding as a motor position (percent * 2), which is why
# driving such a device with a position command sets its brightness.  26 and 31
# are the other two dimmer variants and use the same encoding.
LIGHT_DIMMER_DEVICE_TYPES: set[str] = {"26", "28", "31"}

# Every device type this integration creates entities for.  Note this is a
# strict superset of COVER_DEVICE_TYPES: widening the filter can only ever add
# devices, never drop one that was accepted before.
SUPPORTED_DEVICE_TYPES: set[str] = COVER_DEVICE_TYPES | LIGHT_DIMMER_DEVICE_TYPES

# Device types that support slat tilt: the in-wall actuators used for Raffstoren
# (20/2E) and the slat-roof motor (2A), whose louvres also tilt. Plug receiver
# (21/30) and radio motor (25) drive awnings/roller shutters without slats, so
# tilt is not exposed for them.
#
# Used ONLY as a fallback when we cannot read the per-device productType from
# Block 37 (e.g. motor asleep during config_flow). The authoritative answer
# comes from is_with_blinds in the device's productParameters.
TILT_DEVICE_TYPES: set[str] = {"20", "2A", "2E"}

# Backwards-compatible alias for the cover set.
BLIND_DEVICE_TYPES = COVER_DEVICE_TYPES


def device_type_name(device_type: str | None) -> str:
    """Friendly name for a device type, or '<unknown>' when unlisted."""
    if not device_type:
        return "<unknown>"
    return DEVICE_TYPE_STRINGS.get(device_type.upper(), "<unknown>")


def is_cover_device(device_type: str | None) -> bool:
    """Return True when this device type drives a cover."""
    return bool(device_type) and device_type.upper() in COVER_DEVICE_TYPES


def is_light_device(device_type: str | None) -> bool:
    """Return True when this device type drives a light."""
    return bool(device_type) and device_type.upper() in LIGHT_DIMMER_DEVICE_TYPES


def is_supported_device(device_type: str | None) -> bool:
    """Return True when this integration creates entities for this type."""
    return bool(device_type) and device_type.upper() in SUPPORTED_DEVICE_TYPES


def platform_for_device_type(device_type: str | None) -> str | None:
    """Return the HA platform for a device type, or None when unsupported."""
    if is_cover_device(device_type):
        return "cover"
    if is_light_device(device_type):
        return "light"
    return None
