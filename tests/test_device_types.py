"""Device-type table tests.

Stdlib only, no Home Assistant import. Pins the routing of the device types
that reporters confirmed on real hardware, so widening the table can never
silently drop one of them.
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

from pywarema.device_types import (  # noqa: E402
    device_type_name,
    is_cover_device,
    is_supported_device,
    platform_for_device_type,
)
from pywarema.protocol import decode_frame  # noqa: E402


class DeviceTypeRoutingTest(unittest.TestCase):
    def test_known_cover_types_route_to_cover(self):
        for dt in ("20", "21", "25", "2A", "2E", "30"):
            with self.subTest(device_type=dt):
                self.assertTrue(is_cover_device(dt))
                self.assertEqual(platform_for_device_type(dt), "cover")

    def test_type_30_is_named_and_supported(self):
        # Issue #18: newer plug receiver generation, previously "<unknown>".
        self.assertTrue(is_supported_device("30"))
        self.assertNotEqual(device_type_name("30"), "<unknown>")

    def test_scan_response_from_type_30(self):
        # Scan response captured in issue #18 (serial from the report).
        msg = decode_frame(
            "{r3029267021963A308FFF0300000000000000000003040001FF0300FFFFFFFFFF}"
        )
        self.assertEqual(msg["msg_type"], "scanResponse")
        self.assertEqual(msg["snr"], "302926")
        self.assertEqual(msg["params"]["pan_id"], "963A")
        self.assertEqual(msg["params"]["device_type"], "30")
        self.assertNotEqual(msg["params"]["device_type_str"], "<unknown>")

    def test_unknown_type_stays_unknown(self):
        self.assertFalse(is_cover_device("99"))
        self.assertIsNone(platform_for_device_type("99"))
        self.assertEqual(device_type_name("99"), "<unknown>")


if __name__ == "__main__":
    unittest.main()
