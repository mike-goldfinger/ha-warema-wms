"""Frame tests for the stick-side connection-quality query.

Stdlib only, no Home Assistant import. ``{Q<snr>}`` asks the stick to probe
the radio link to one device; it answers ``{q<snr><txLQI><txRSSI><rxLQI><rxRSSI>}``.
"""

import os
import sys
import types
import unittest

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

from pywarema.protocol import decode_frame, encode_cmd  # noqa: E402

SNR = 994624  # hex LE 402D0F


class ConnectionQualityFrameTest(unittest.TestCase):
    def test_encode_request(self):
        enc = encode_cmd("connectionQuality", SNR, {})
        self.assertEqual(enc["cmd"], "{Q402D0F}")
        self.assertEqual(enc["expect"]["msg_type"], "connectionQuality")
        self.assertEqual(enc["expect"]["snr"], "402D0F")

    def test_decode_response(self):
        msg = decode_frame("{q402D0FC8A5B09B}")
        self.assertEqual(msg["msg_type"], "connectionQuality")
        self.assertEqual(msg["snr"], "402D0F")
        self.assertEqual(msg["snr_num"], SNR)
        self.assertEqual(
            msg["params"],
            {"tx_lqi": 0xC8, "tx_rssi": 0xA5, "rx_lqi": 0xB0, "rx_rssi": 0x9B},
        )

    def test_decode_short_frame_is_unknown(self):
        msg = decode_frame("{q402D0F}")
        self.assertEqual(msg["msg_type"], "unknown")
        self.assertEqual(msg["params"], {})

    def test_existing_frames_unaffected(self):
        self.assertEqual(decode_frame("{a}")["msg_type"], "ack")
        self.assertEqual(
            decode_frame("{r402D0F801101000005C8FFFFFF00}")["msg_type"], "position"
        )


if __name__ == "__main__":
    unittest.main()
