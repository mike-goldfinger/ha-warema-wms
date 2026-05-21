"""
pywarema - Python library for Warema WMS (WAREMA Mobile System) radio control.

Ports the serial communication protocol from the JavaScript
warema-wms-venetian-blinds npm package to Python.

Serial port settings: 125000 baud, 8N1
Protocol: ASCII frames delimited by '}' character, enclosed in '{...}'
"""

from .stick import WmsStick
from .protocol import (
    snr_num_to_hex,
    snr_hex_to_num,
    encode_cmd,
    decode_frame,
    pos_hex_to_percent,
    pos_percent_to_hex,
    angle_hex_to_percent,
    angle_percent_to_hex,
)

__version__ = "1.0.0"
__all__ = [
    "WmsStick",
    "snr_num_to_hex",
    "snr_hex_to_num",
    "encode_cmd",
    "decode_frame",
    "pos_hex_to_percent",
    "pos_percent_to_hex",
    "angle_hex_to_percent",
    "angle_percent_to_hex",
]
