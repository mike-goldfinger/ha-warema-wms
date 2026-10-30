"""Tests for re-scaling a decoded position frame's angle to a motor's real
range once it is known (issue #7).

``decode_frame()`` has no per-device context, so it scales ``angle_raw`` with
the default (venetian-blind) -75..+75 range. ``WmsStick._rescale_angle``
corrects that once a motor's real range (``blind.min_angle``/``max_angle``)
has been read - see ``_read_slat_roof_angle_range``.
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

from pywarema.protocol import angle_hex_to_percent  # noqa: E402
from pywarema.stick import Blind, WmsStick  # noqa: E402


def make_blind(min_angle=None, max_angle=None) -> Blind:
    return Blind(
        snr=1984827,
        snr_hex="3B491E",
        name="Slat roof",
        min_angle=min_angle,
        max_angle=max_angle,
    )


def make_stick() -> WmsStick:
    return WmsStick(
        port="/dev/null",
        channel=17,
        pan_id="ABCD",
        key="0123456789ABCDEF0123456789ABCDEF",
        callback=lambda *_: None,
        auto_open=False,
    )


class TestRescaleAngle(unittest.TestCase):
    def test_angle_none_stays_none(self):
        blind = make_blind(min_angle=-45, max_angle=90)
        self.assertIsNone(
            WmsStick._rescale_angle(blind, {"angle": None, "angle_raw": 0xFF})
        )

    def test_range_unknown_returns_default_decoded_angle_unchanged(self):
        blind = make_blind()  # min_angle/max_angle still None (not yet read)
        params = {"angle": -60, "angle_raw": 0x52}
        self.assertEqual(WmsStick._rescale_angle(blind, params), -60)

    def test_range_known_rescales_from_raw_byte(self):
        blind = make_blind(min_angle=-45, max_angle=90)
        # angle_raw=0x52 (82) is the real hardware value from issue #7's log
        # at the motor's fully-closed end stop; the default-scaled "angle"
        # (-60, as decode_frame would have produced) must be discarded in
        # favor of re-deriving it from angle_raw with the real range.
        params = {"angle": -60, "angle_raw": 0x52}
        self.assertEqual(WmsStick._rescale_angle(blind, params), -100)

    def test_matches_angle_hex_to_percent_with_same_range(self):
        blind = make_blind(min_angle=-45, max_angle=90)
        for raw in (0x52, 0x95, 0xCA, 0xD9):
            params = {"angle": 0, "angle_raw": raw}  # "angle" value irrelevant
            expected = angle_hex_to_percent(
                format(raw, "02X"), min_angle=-45, max_angle=90
            )
            self.assertEqual(WmsStick._rescale_angle(blind, params), expected)

    def test_missing_raw_byte_falls_back_to_default_decoded_angle(self):
        blind = make_blind(min_angle=-45, max_angle=90)
        params = {"angle": -60}  # no angle_raw key
        self.assertEqual(WmsStick._rescale_angle(blind, params), -60)


class TestReadSlatRoofAngleRange(unittest.TestCase):
    """_read_slat_roof_angle_range() must reject a degenerate range rather
    than adopting it - see angle_hex_to_percent()'s division by span.
    """

    def setUp(self):
        self.stick = make_stick()
        self.blind = self.stick.blind_add(1984827, "Slat roof")

    def _mock_mb8_read(self, raw_bytes: bytes):
        def fake_mb8_read(blind_id, block, addr, size, on_complete=None):
            on_complete("", None, {"params": {"data": raw_bytes}})

        return patch.object(self.stick, "mb8_read", side_effect=fake_mb8_read)

    def test_plausible_range_is_adopted(self):
        # 82, 217 = the real minAngle/maxAngle bytes from issue #7 / Studio's
        # dataTypeId 292 ("angle-45+90").
        with self._mock_mb8_read(bytes([82, 217])):
            self.stick._read_slat_roof_angle_range(self.blind, timeout=1.0)
        self.assertEqual(self.blind.min_angle, -45)
        self.assertEqual(self.blind.max_angle, 90)

    def test_equal_bounds_are_rejected(self):
        with self._mock_mb8_read(bytes([100, 100])):
            self.stick._read_slat_roof_angle_range(self.blind, timeout=1.0)
        self.assertIsNone(self.blind.min_angle)
        self.assertIsNone(self.blind.max_angle)

    def test_reversed_bounds_are_rejected(self):
        with self._mock_mb8_read(bytes([200, 50])):
            self.stick._read_slat_roof_angle_range(self.blind, timeout=1.0)
        self.assertIsNone(self.blind.min_angle)
        self.assertIsNone(self.blind.max_angle)


if __name__ == "__main__":
    unittest.main()
