"""Decoding tests for the weather-station broadcast (rcv type 7080).

Stdlib only, no Home Assistant import. The frames come from issue #17: a
*Wetterstation eco* (wind + brightness only) whose unused sensor bytes are
``FF``. Before the fix those bytes were decoded unconditionally and produced
a constant 92.5 °C temperature.
"""

import os
import sys
import types
import unittest

# pywarema/__init__ imports the stick, which needs pyserial. Home Assistant
# supplies it in production; stub it here so the pure-Python protocol module
# can be imported without it.
sys.modules.setdefault(
    "serial", types.SimpleNamespace(Serial=object, SerialException=Exception)
)

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "custom_components",
        "warema_wms",
    ),
)

from pywarema.protocol import decode_frame  # noqa: E402

SNR_HEX = "13310F"
PAD = "FF" * 23


def _frame(payload: str) -> str:
    """Build a 7080 frame; payload = bytes 0..9, the rest is padded with FF.

    Byte layout (issue #17): 0 unknown, 1 wind, 2 lumen hi, 3-5 unknown,
    6 lumen lo, 7 battery, 8 rain, 9 temperature.
    """
    return "{r" + SNR_HEX + "7080" + payload + PAD + "}"


class WeatherBroadcastDecodeTest(unittest.TestCase):
    def test_eco_station_no_temp_no_rain(self):
        # Issue #17: wind 0, brightness 0, battery C0 = 96 %, temp/rain FF.
        msg = decode_frame(_frame("000000FFFFFF00C0FFFF"))
        self.assertEqual(msg["msg_type"], "weatherBroadcast")
        p = msg["params"]
        self.assertEqual(p["wind"], 0)
        self.assertEqual(p["lumen"], 0)
        self.assertEqual(p["battery"], 96)
        self.assertIsNone(p["temp"])
        self.assertIsNone(p["rain"])

    def test_wind_half_steps_and_full_battery(self):
        # WMS Studio showed 0.5 m/s and 100 % for this frame.
        p = decode_frame(_frame("000100FFFFFF00C8FFFF"))["params"]
        self.assertEqual(p["wind"], 0.5)
        self.assertEqual(p["battery"], 100)

    def test_full_station_values(self):
        # Wind 1.5 m/s, temp 0x50 = 80/2 - 35 = 5.0 °C, rain C8 = raining.
        p = decode_frame(_frame("000300FFFFFF00C8C850"))["params"]
        self.assertEqual(p["wind"], 1.5)
        self.assertEqual(p["temp"], 5.0)
        self.assertIs(p["rain"], True)
        self.assertEqual(p["battery"], 100)

    def test_rain_flag_off(self):
        p = decode_frame(_frame("000000FFFFFF00C80050"))["params"]
        self.assertIs(p["rain"], False)

    def test_wind_sensor_missing(self):
        p = decode_frame(_frame("00FF00FFFFFF00C8FFFF"))["params"]
        self.assertIsNone(p["wind"])


if __name__ == "__main__":
    unittest.main()
