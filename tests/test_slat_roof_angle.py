"""Tests for device-specific tilt-angle scaling (issue #7).

Slat-roof motors (Lamellendach, product types 27/28/29) have a real tilt
range of -45..+90 degrees (WMS Studio Pro dataTypeId 292, "angle-45+90"),
not the -75..+75 assumed by the previous fixed-constant implementation
(dataTypeId 263 for venetian blinds, "angle0+80": 0..+80 - itself not
symmetric either, but the default keeps the historical behaviour). Verified
against the decrypted WMS Studio Pro PLists and against real hardware logs
from issue #7 (motor 3B491E: byte 0x52=82 at fully closed, 0xCA=202/0xD9=217
near/at fully open).

Stdlib only and no Home Assistant import: ``pywarema.protocol`` is pure
Python, so this runs anywhere with ``python -m unittest discover tests``.
"""

import os
import sys
import types
import unittest

sys.modules.setdefault("serial", types.SimpleNamespace(Serial=object))

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "custom_components",
        "warema_wms",
    ),
)

from pywarema.protocol import (  # noqa: E402
    WMS_ANGLE,
    angle_hex_to_percent,
    angle_percent_to_hex,
    encode_cmd,
    pos_percent_to_hex,
)

SNR = 1984827
SNR_HEX = "3B491E"


class TestDefaultRangeUnchanged(unittest.TestCase):
    """The default (no min_angle/max_angle given) must match the old ±75° formula."""

    def test_encode_matches_old_formula_for_every_percent(self):
        for pct in range(-100, 101):
            raw = round(pct / 100 * WMS_ANGLE)
            clamped = max(-WMS_ANGLE, min(WMS_ANGLE, raw))
            expected = format(clamped + 127, "02X")
            self.assertEqual(angle_percent_to_hex(pct), expected)

    def test_decode_matches_old_formula_for_every_byte(self):
        for byte in range(256):
            hexs = format(byte, "02X")
            expected = round((byte - 127) / WMS_ANGLE * 100)
            self.assertEqual(angle_hex_to_percent(hexs), expected)


class TestSlatRoofRange(unittest.TestCase):
    """Slat-roof motors use dataTypeId 292 ("angle-45+90"): bytes 82..217."""

    def test_endpoints_match_studio_datatype_292(self):
        self.assertEqual(
            angle_percent_to_hex(-100, min_angle=-45, max_angle=90), "52"
        )  # 82 dec = studio minValue
        self.assertEqual(
            angle_percent_to_hex(100, min_angle=-45, max_angle=90), "D9"
        )  # 217 dec = studio maxValue

    def test_decode_endpoints(self):
        self.assertEqual(angle_hex_to_percent("52", min_angle=-45, max_angle=90), -100)
        self.assertEqual(angle_hex_to_percent("D9", min_angle=-45, max_angle=90), 100)

    def test_real_hardware_log_from_issue_7(self):
        # Motor 3B491E: byte 0x52 (82) at the physical fully-closed end stop.
        self.assertEqual(angle_hex_to_percent("52", min_angle=-45, max_angle=90), -100)
        # byte 0xCA (202) near the physical fully-open end stop - inside the
        # firmware's configured range (82..217), so a percentage below 100,
        # not the previous implementation's out-of-range / wrongly-scaled
        # values (it decoded 0x52 as -60% and 0xCA as 100%, losing the
        # asymmetry entirely).
        pct = angle_hex_to_percent("CA", min_angle=-45, max_angle=90)
        self.assertGreater(pct, 0)
        self.assertLess(pct, 100)

    def test_round_trip(self):
        for pct in range(-100, 101, 5):
            hexs = angle_percent_to_hex(pct, min_angle=-45, max_angle=90)
            back = angle_hex_to_percent(hexs, min_angle=-45, max_angle=90)
            self.assertLessEqual(abs(back - pct), 1)


class TestPositionLessTiltCommand(unittest.TestCase):
    """Tilt-only actuators (no position axis) need pos=None to send 0xFF.

    Verified against WMS Studio Pro's INVALID_MANUAL_CMD_SETTINGS
    (setting0..3 = 255), the sentinel it uses for "leave this byte alone" on
    every settings byte of a manual command frame, not just the angle.
    """

    def test_pos_none_encodes_as_sentinel(self):
        self.assertEqual(pos_percent_to_hex(None), "FF")

    def test_pos_none_leaves_position_byte_untouched_in_full_frame(self):
        frame = encode_cmd("blindMoveToPos", SNR, {"pos": None, "ang": -100})["cmd"]
        # {R06<snr>7070 03 <pos> <ang> <valance1> <valance2>}
        self.assertEqual(frame, "{R06" + SNR_HEX + "70700" + "3FF34FFFF}")

    def test_pos_none_and_ang_none_is_all_sentinels(self):
        frame = encode_cmd("blindMoveToPos", SNR, {"pos": None, "ang": None})["cmd"]
        self.assertEqual(frame, "{R06" + SNR_HEX + "70700" + "3FFFFFFFF}")


class TestEncodeCmdUsesRealRange(unittest.TestCase):
    """blindMoveToPos must encode outgoing angles with the motor's real
    range when given one, not just the default - otherwise HA tilt=100%
    would command the wrong physical angle on a slat roof (it would land on
    the venetian-blind default's +75°, not the slat roof's real +90° end
    stop), even though incoming readings are correctly rescaled.
    """

    def _angle_byte(self, frame: str) -> str:
        # {R06<snr>7070 03 <pos> <ang> <valance1> <valance2>}
        return frame[18:20]

    def test_open_100_percent_with_default_range(self):
        frame = encode_cmd("blindMoveToPos", SNR, {"pos": None, "ang": 100})["cmd"]
        self.assertEqual(self._angle_byte(frame), angle_percent_to_hex(100))

    def test_open_100_percent_with_slat_roof_range(self):
        frame = encode_cmd(
            "blindMoveToPos",
            SNR,
            {"pos": None, "ang": 100, "min_angle": -45, "max_angle": 90},
        )["cmd"]
        self.assertEqual(
            self._angle_byte(frame),
            angle_percent_to_hex(100, min_angle=-45, max_angle=90),
        )
        # The whole point: this must NOT be the default range's byte, or a
        # slat roof's "open" command would stop short of its real end stop.
        self.assertNotEqual(self._angle_byte(frame), angle_percent_to_hex(100))

    def test_partial_range_falls_back_to_default_for_missing_bound(self):
        # Only min_angle given: max_angle must still fall back to the
        # default WMS_ANGLE, not to some other value.
        frame = encode_cmd(
            "blindMoveToPos", SNR, {"pos": None, "ang": 100, "min_angle": -45}
        )["cmd"]
        self.assertEqual(
            self._angle_byte(frame),
            angle_percent_to_hex(100, min_angle=-45, max_angle=WMS_ANGLE),
        )


class TestDegenerateRangeIsSafe(unittest.TestCase):
    """A misread device (min_angle == max_angle) must not crash decoding.

    _read_slat_roof_angle_range() rejects this before it ever reaches
    blind.min_angle/max_angle (see test_slat_roof_rescale.py), but the pure
    encode/decode functions guard against it too, in case they are ever
    called directly with an untrusted range.
    """

    def test_equal_bounds_returns_zero_instead_of_raising(self):
        self.assertEqual(angle_hex_to_percent("80", min_angle=10, max_angle=10), 0)


if __name__ == "__main__":
    unittest.main()
