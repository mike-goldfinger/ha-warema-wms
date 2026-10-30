"""Constants for the Warema WMS integration."""

DOMAIN = "warema_wms"

# Config entry keys
CONF_SERIAL_PORT = "serial_port"
CONF_CHANNEL = "channel"
CONF_PAN_ID = "pan_id"
CONF_NETWORK_KEY = "network_key"
CONF_DEVICES = "devices"

# Options entry keys
# Per-device position inversion. Stored under entry.options as a dict keyed by
# the device's string SNR -> bool. When True, the cover's open/closed direction
# is mirrored in HA (display + commands) without touching the motor. Intended
# for awnings (Markisen), where the retracted/home position is intuitively
# "closed" while WMS reports it as 0 (= HA "open"). This is a HA-side display
# convention only; the physical equivalent is the motor_rotation firmware param.
OPT_INVERT_POSITION = "invert_position"

# Default values
DEFAULT_SERIAL_PORT = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AV0K28M2-if00-port0"
DEFAULT_CHANNEL = 17

# Position polling interval (seconds)
# Note: Determines how fast remote control moves are detected.
# Shorter interval = faster detection but more network traffic.
POS_UPDATE_INTERVAL = 5

# Watch moving blinds interval (seconds)
WATCH_MOVING_INTERVAL = 0.5

# Discovery wizard
CONF_DISCOVERY_MODE = "discovery_mode"
DISCOVERY_MODE_MANUAL = "manual"
DISCOVERY_MODE_WANDSENDER = "wandsender"
DISCOVERY_MODE_NEW_NETWORK = "new_network"

# Default channel for new network creation
DEFAULT_NEW_NETWORK_CHANNEL = 24

# Topics from pywarema callback
TOPIC_INIT_COMPLETION = "wms-vb-init-completion"
TOPIC_BLIND_POSITION_UPDATE = "wms-vb-blind-position-update"
TOPIC_SCANNED_DEVICES = "wms-vb-scanned-devices"
TOPIC_WEATHER_BROADCAST = "wms-vb-rcv-weather-broadcast"

# Dispatcher signal fired when a previously unseen weather station broadcasts.
# Carries the station's integer SNR; formatted per config entry at use site.
SIGNAL_NEW_WEATHER_STATION = f"{DOMAIN}_new_weather_station"

# Device type table, platform routing and the tilt fallback set live in
# pywarema.device_types (single source of truth, importable without HA).
# Re-exported here because the HA-side modules import them from const.
from .pywarema.device_types import (  # noqa: F401  (re-export)
    BLIND_DEVICE_TYPES,
    COVER_DEVICE_TYPES,
    DEVICE_TYPE_STRINGS,
    LIGHT_DEVICE_TYPES,
    LIGHT_DIMMER_DEVICE_TYPES,
    LIGHT_SWITCH_DEVICE_TYPES,
    SUPPORTED_DEVICE_TYPES,
    TILT_DEVICE_TYPES,
    device_type_name,
    is_cover_device,
    is_light_device,
    is_supported_device,
    is_switch_light_device,
    platform_for_device_type,
)

# Map productType (from motor Block 37 addr 12) to HA CoverDeviceClass strings.
# Kept as strings here to avoid importing homeassistant from const.py - cover.py
# wraps them in CoverDeviceClass(...). Anything not listed falls through to
# CoverDeviceClass.BLIND.
#
# See pywarema.protocol.PRODUCT_TYPE_NAMES for the full productType table.
PRODUCT_TYPE_TO_DEVICE_CLASS: dict[int, str] = {
    0: "blind",  # ExternalVenetianBlind (Raffstore)
    1: "blind",  # InternalVenetianBlind
    2: "shutter",  # RollerShutter (Rollladen)
    3: "awning",  # Awning
    4: "awning",  # AwningOneValance
    5: "awning",  # AwningOneOrTwoWindsensors
    6: "awning",  # AwningOneValanceOneOrTwoWindsensors
    7: "awning",  # ConservatoryAwning (Wintergarten)
    8: "awning",  # FacadeAwning (Fassadenmarkise)
    9: "awning",  # DroparmAwning (Gelenkarm)
    10: "awning",  # VerticalAwning (Senkrechtmarkise)
    11: "awning",  # Markisolette
    12: "shade",  # PleatedBlindInside (Plissee)
    13: "shade",  # RollerBlindInside (Innenrollo)
    14: "blind",  # VerticalLouvreBlind (Vertikallamellen)
    15: "window",  # Window
    21: "awning",  # Valance (Volant)
    22: "awning",  # AwningTwoValances
    23: "awning",  # AwningTwoValancesOneOrTwoWindsensors
    24: "awning",  # SunSail
    25: "awning",  # PergolaAwning
    27: "awning",  # SlatRoofL60 (Lamellendach)
    28: "awning",  # SlatRoofL70 (Lamellendach)
    29: "awning",  # SlatRoofL70Tilting (Lamellendach)
}
