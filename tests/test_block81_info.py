"""Tests for decoding Block 81 (firmware/device-type info).

Layout verified against WMS Studio Pro's own read of this block
(ReadDeviceMetaDataConvBlock81Conv): 32 bytes starting at address 24, with
the software version as an 11-char Latin-1 string at offset 0 and the
device-type byte at offset 19. See issue #8.
"""

import os
import sys
import types
import unittest
from unittest.mock import patch

sys.modules.setdefault("serial", types.SimpleNamespace(Serial=object))

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "custom_components",
        "warema_wms",
    ),
)

from pywarema.protocol import SW_INFO_ADDR, SW_INFO_BLOCK, SW_INFO_SIZE  # noqa: E402
from pywarema.stick import WmsStick  # noqa: E402


class TestBlock81Info(unittest.TestCase):
    """read_block81_info must use the Studio-verified address and offsets."""

    def setUp(self):
        self.stick = WmsStick(
            port="/dev/null",
            channel=17,
            pan_id="ABCD",
            key="0123456789ABCDEF0123456789ABCDEF",
            callback=lambda *_: None,
            auto_open=False,
        )
        self.stick.blind_add(1984827, "Test motor")

    def test_reads_studio_verified_address_and_size(self):
        with patch.object(
            self.stick, "_mb8_read_sync", return_value=bytes(32)
        ) as mock_read:
            self.stick.read_block81_info(1984827)

        mock_read.assert_called_once()
        _, kwargs = mock_read.call_args
        self.assertEqual(kwargs["block"], SW_INFO_BLOCK)
        self.assertEqual(kwargs["addr"], SW_INFO_ADDR)
        self.assertEqual(kwargs["size"], SW_INFO_SIZE)
        self.assertEqual(SW_INFO_ADDR, 24)

    def test_decodes_software_version_and_device_type(self):
        # Software version "05930141007" (11 Latin-1 chars) followed by
        # padding, with the device-type byte (0x26 = Dimmer) at offset 19.
        raw = bytearray(b"\xff" * 32)
        raw[0:11] = b"05930141007"
        raw[19] = 0x26

        with patch.object(self.stick, "_mb8_read_sync", return_value=bytes(raw)):
            sw_ver, dev_type = self.stick.read_block81_info(1984827)

        self.assertEqual(sw_ver, "05930141007")
        self.assertEqual(dev_type, "0x26")

    def test_no_response_returns_none_tuple(self):
        with patch.object(self.stick, "_mb8_read_sync", return_value=None):
            sw_ver, dev_type = self.stick.read_block81_info(1984827)

        self.assertIsNone(sw_ver)
        self.assertIsNone(dev_type)


if __name__ == "__main__":
    unittest.main()
