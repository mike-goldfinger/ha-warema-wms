"""Constants for the Warema WMS integration."""

DOMAIN = "warema_wms"

# Config entry keys
CONF_SERIAL_PORT = "serial_port"
CONF_CHANNEL = "channel"
CONF_PAN_ID = "pan_id"
CONF_NETWORK_KEY = "network_key"
CONF_DEVICES = "devices"

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

# Device type strings
DEVICE_TYPE_STRINGS = {
    "02": "Stick/software",
    "06": "Weather station",
    "07": "Remote control (+)",
    "20": "Actuator UP",
    "21": "Plug receiver",
    "25": "Radio motor",
    "2E": "Actuator 230V UP",
    "63": "Web control",
}

# Blind device types (controllable covers)
BLIND_DEVICE_TYPES = {"20", "21", "25", "2E"}

# Device types that support slat tilt (in-wall actuators used for Raffstoren).
# Plug receiver (21) and radio motor (25) drive awnings/roller shutters
# without slats, so tilt is not exposed for them.
TILT_DEVICE_TYPES = {"20", "2E"}
