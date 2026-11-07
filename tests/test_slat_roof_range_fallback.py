"""Tests for the slat-roof tilt-range fallback and the deferred range read
(issue #7, v1.9.0-beta.3 feedback).

A slat-roof motor's real tilt range is read from block 38 once at startup.
That read often finds the motor asleep and times out, after which every tilt
command was scaled with the venetian-blind default (-75..+75): HA "100 %"
became byte 52, below the louvres' travel (82..217), and the motor
acknowledged such a target but did not move. Two things fix that:

1. While the range is unread, slat-roof products fall back to the louvre
   data type's full span (-45..+90) instead of -75..+75, so every target
   byte is inside the motor's travel.
2. The range read is retried (non-blocking) right after the motor answered a
   position poll - i.e. while it is awake - until it succeeds or the retry
   budget is spent.
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

from pywarema.protocol import (  # noqa: E402
    SLAT_ROOF_DEFAULT_MAX_ANGLE,
    SLAT_ROOF_DEFAULT_MIN_ANGLE,
    default_angle_range,
    encode_cmd,
)
from pywarema.stick import (  # noqa: E402
    SLAT_ROOF_RANGE_READ_ATTEMPTS,
    Blind,
    WmsStick,
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


def angle_byte(frame: str) -> int:
    """Return the slat-angle byte of a blindMoveToPos frame."""
    # {R06<snr 6>7070 03 <pos 2> <ang 2> <v1 2> <v2 2>}
    return int(frame[18:20], 16)


class TestDefaultAngleRange(unittest.TestCase):
    def test_slat_roof_products_get_louvre_span(self):
        for product_type in (27, 28, 29):
            self.assertEqual(
                default_angle_range(product_type),
                (SLAT_ROOF_DEFAULT_MIN_ANGLE, SLAT_ROOF_DEFAULT_MAX_ANGLE),
            )

    def test_other_products_keep_generic_default(self):
        self.assertIsNone(default_angle_range(1))
        self.assertIsNone(default_angle_range(None))

    def test_fallback_matches_louvre_data_type_bytes(self):
        # Data type "angle-45+90": bytes 82..217.
        self.assertEqual(SLAT_ROOF_DEFAULT_MIN_ANGLE + 127, 82)
        self.assertEqual(SLAT_ROOF_DEFAULT_MAX_ANGLE + 127, 217)


class TestEffectiveAngleRange(unittest.TestCase):
    def setUp(self):
        self.stick = make_stick()

    def test_unread_slat_roof_uses_fallback(self):
        blind = Blind(snr=1984827, snr_hex="3B491E", name="roof", product_type=27)
        self.assertEqual(WmsStick._angle_range(blind), (-45, 90))

    def test_read_range_wins_over_fallback(self):
        blind = Blind(
            snr=1984827,
            snr_hex="3B491E",
            name="roof",
            product_type=27,
            min_angle=-45,
            max_angle=75,
        )
        self.assertEqual(WmsStick._angle_range(blind), (-45, 75))

    def test_unread_venetian_blind_has_no_range(self):
        blind = Blind(snr=1, snr_hex="000001", name="blind", product_type=1)
        self.assertIsNone(WmsStick._angle_range(blind))

    def test_move_command_on_unread_slat_roof_stays_inside_louvre_travel(self):
        blind = self.stick.blind_add(1984827, "roof")
        blind.product_type = 27
        self.stick.blind_set_position(blind.snr, position=None, angle=-100)
        msg = self.stick._msg_queue[0]
        frame = encode_cmd(msg.cmd, msg.snr, msg.params)["cmd"]
        # -100 % -> the louvres' lower end (82), not the venetian-blind 52
        # that the motor acknowledges and then ignores.
        self.assertEqual(angle_byte(frame), 82)

    def test_move_command_on_unread_slat_roof_upper_end(self):
        blind = self.stick.blind_add(1984827, "roof")
        blind.product_type = 27
        self.stick.blind_set_position(blind.snr, position=None, angle=100)
        msg = self.stick._msg_queue[0]
        frame = encode_cmd(msg.cmd, msg.snr, msg.params)["cmd"]
        self.assertEqual(angle_byte(frame), 217)

    def test_move_command_uses_read_range_when_known(self):
        blind = self.stick.blind_add(1984827, "roof")
        blind.product_type = 27
        blind.min_angle, blind.max_angle = -45, 75  # one real L60 (issue #7)
        self.stick.blind_set_position(blind.snr, position=None, angle=100)
        msg = self.stick._msg_queue[0]
        frame = encode_cmd(msg.cmd, msg.snr, msg.params)["cmd"]
        self.assertEqual(angle_byte(frame), 202)

    def test_move_command_on_venetian_blind_is_unchanged(self):
        blind = self.stick.blind_add(1234567, "blind")
        blind.product_type = 1
        self.stick.blind_set_position(blind.snr, position=50, angle=-100)
        msg = self.stick._msg_queue[0]
        frame = encode_cmd(msg.cmd, msg.snr, msg.params)["cmd"]
        self.assertEqual(angle_byte(frame), 52)

    def test_rescale_on_unread_slat_roof_uses_fallback(self):
        blind = Blind(snr=1984827, snr_hex="3B491E", name="roof", product_type=27)
        # Raw 82 = the louvres' lower end: -100 %, not the -60 % the
        # venetian-blind default decodes it to.
        self.assertEqual(
            WmsStick._rescale_angle(blind, {"angle": -60, "angle_raw": 82}), -100
        )
        self.assertEqual(
            WmsStick._rescale_angle(blind, {"angle": 100, "angle_raw": 217}), 100
        )


class TestDeferredRangeRead(unittest.TestCase):
    def setUp(self):
        self.stick = make_stick()
        self.blind = self.stick.blind_add(1984827, "roof")
        self.blind.product_type = 27
        self.reads: list = []

    def _mock_mb8_read(self):
        def fake_mb8_read(blind_id, block, addr, size, on_complete=None):
            self.reads.append((blind_id, block, addr, size, on_complete))

        return patch.object(self.stick, "mb8_read", side_effect=fake_mb8_read)

    def test_read_is_queued_once_while_range_unknown(self):
        with self._mock_mb8_read():
            self.assertTrue(self.stick._request_slat_roof_angle_range(self.blind))
            # A second request while the first is in flight is a no-op.
            self.assertFalse(self.stick._request_slat_roof_angle_range(self.blind))
        self.assertEqual(len(self.reads), 1)
        _, block, addr, size, _ = self.reads[0]
        self.assertEqual((block, addr, size), (38, 463, 2))

    def test_successful_read_adopts_range_and_stops_retrying(self):
        with self._mock_mb8_read():
            self.stick._request_slat_roof_angle_range(self.blind)
            on_complete = self.reads[0][4]
            on_complete("", None, {"params": {"data": bytes([82, 202])}})
            self.assertEqual((self.blind.min_angle, self.blind.max_angle), (-45, 75))
            self.assertFalse(self.blind.angle_range_pending)
            self.assertFalse(self.stick._request_slat_roof_angle_range(self.blind))
        self.assertEqual(len(self.reads), 1)

    def test_timeout_allows_another_attempt(self):
        with self._mock_mb8_read():
            self.stick._request_slat_roof_angle_range(self.blind)
            self.reads[0][4]("timeout", None, None)
            self.assertIsNone(self.blind.min_angle)
            self.assertTrue(self.stick._request_slat_roof_angle_range(self.blind))
        self.assertEqual(len(self.reads), 2)

    def test_retry_budget_is_finite(self):
        with self._mock_mb8_read():
            for _ in range(SLAT_ROOF_RANGE_READ_ATTEMPTS):
                self.assertTrue(self.stick._request_slat_roof_angle_range(self.blind))
                self.reads[-1][4]("timeout", None, None)
            self.assertFalse(self.stick._request_slat_roof_angle_range(self.blind))
        self.assertEqual(len(self.reads), SLAT_ROOF_RANGE_READ_ATTEMPTS)

    def test_no_read_for_products_without_slat_roof_layout(self):
        blind = self.stick.blind_add(1234567, "blind")
        blind.product_type = 1
        with self._mock_mb8_read():
            self.assertFalse(self.stick._request_slat_roof_angle_range(blind))
        self.assertEqual(self.reads, [])

    def test_position_reply_triggers_the_read(self):
        with self._mock_mb8_read():
            self.stick.blind_get_position(self.blind.snr)
            msg = self.stick._msg_queue[0]
            self.assertEqual(msg.cmd, "blindGetPos")
            msg.on_end(
                "",
                msg,
                {
                    "params": {
                        "position": -1,
                        "angle": -60,
                        "angle_raw": 82,
                        "position_raw": 255,
                        "moving": False,
                    }
                },
            )
        self.assertEqual(len(self.reads), 1)


if __name__ == "__main__":
    unittest.main()
