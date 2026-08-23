"""Frame-encoding tests for valance support.

Stdlib only and no Home Assistant import: ``pywarema.protocol`` is pure
Python, so this runs anywhere with ``python -m unittest discover tests``.

The point of these tests is the backwards-compatibility guarantee. Valance
support widens an existing frame, so the first test class pins the bytes that
every pre-existing call site produces. If those ever change, hardware that has
no valance would start receiving different commands.
"""

import os
import sys
import unittest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "custom_components",
        "warema_wms",
    ),
)

from pywarema.protocol import encode_cmd  # noqa: E402

SNR = 995603
SNR_HEX = "13310F"


def move(**params) -> str:
    """Encode a blindMoveToPos frame and return the wire string."""
    return encode_cmd("blindMoveToPos", SNR, params)["cmd"]


class TestUnchangedForDevicesWithoutValance(unittest.TestCase):
    """Every call that predates valance support must emit identical bytes.

    Expected values are the literal output of the previous implementation,
    which ended each frame with a hardcoded "FFFF".
    """

    def test_closed_no_tilt(self):
        self.assertEqual(move(pos=100, ang=0), "{R06" + SNR_HEX + "707003C87FFFFF}")

    def test_open_tilt_out(self):
        self.assertEqual(move(pos=0, ang=-100), "{R06" + SNR_HEX + "7070030034FFFF}")

    def test_mid_position_tilt_in(self):
        self.assertEqual(move(pos=50, ang=100), "{R06" + SNR_HEX + "70700364CAFFFF}")

    def test_trailing_bytes_stay_masked_for_every_pos_and_angle(self):
        # The valance bytes are the last two before the closing brace. Without
        # valance arguments they must read FFFF for every reachable position
        # and angle - that is the whole no-regression guarantee.
        for pos in range(0, 101):
            for ang in range(-100, 101):
                frame = move(pos=pos, ang=ang)
                self.assertEqual(frame[-5:], "FFFF}", f"pos={pos} ang={ang} -> {frame}")

    def test_defaults_are_unchanged(self):
        # No pos/ang at all: both still default to 0.
        self.assertEqual(move(), "{R06" + SNR_HEX + "707003007FFFFF}")

    def test_valance_none_is_same_as_absent(self):
        self.assertEqual(
            move(pos=100, ang=0, valance_1=None, valance_2=None), move(pos=100, ang=0)
        )


class TestValanceEncoding(unittest.TestCase):
    """A valance is encoded like a position (percent * 2)."""

    def test_valance_1_lowered(self):
        self.assertEqual(
            move(pos=100, ang=None, valance_1=100),
            "{R06" + SNR_HEX + "707003C8FFC8FF}",
        )

    def test_valance_1_raised(self):
        self.assertEqual(
            move(pos=100, ang=None, valance_1=0),
            "{R06" + SNR_HEX + "707003C8FF00FF}",
        )

    def test_valance_1_half(self):
        self.assertEqual(
            move(pos=100, ang=None, valance_1=50),
            "{R06" + SNR_HEX + "707003C8FF64FF}",
        )

    def test_valance_2_leaves_valance_1_masked(self):
        self.assertEqual(
            move(pos=100, ang=None, valance_2=25),
            "{R06" + SNR_HEX + "707003C8FFFF32}",
        )

    def test_angle_masked_only_when_explicitly_none(self):
        # Byte layout after the "7070" command and its "03" flags byte:
        # position, angle, valance 1, valance 2.
        with_angle = move(pos=100, ang=0, valance_1=100)
        self.assertEqual(with_angle[18:20], "7F")  # ang=0 is a real angle
        without_angle = move(pos=100, ang=None, valance_1=100)
        self.assertEqual(without_angle[18:20], "FF")  # masked

    def test_valance_is_clamped_like_a_position(self):
        self.assertEqual(
            move(pos=0, ang=None, valance_1=150), move(pos=0, ang=None, valance_1=100)
        )
        self.assertEqual(
            move(pos=0, ang=None, valance_1=-10), move(pos=0, ang=None, valance_1=0)
        )


class TestAgainstKnownWorkingImplementation(unittest.TestCase):
    """Match frames from an independent implementation, verified on hardware.

    These are the exact bytes produced by a long-running Node.js bridge driving
    a Warema awning with a valance (device type 25, SNR 995603) in daily use
    since 2024. It masks the slat angle and writes valance 1 only.
    """

    def test_lower_valance_on_extended_awning(self):
        self.assertEqual(
            move(pos=100, ang=None, valance_1=100), "{R0613310F707003C8FFC8FF}"
        )

    def test_raise_valance_on_extended_awning(self):
        self.assertEqual(
            move(pos=100, ang=None, valance_1=0), "{R0613310F707003C8FF00FF}"
        )


if __name__ == "__main__":
    unittest.main()
