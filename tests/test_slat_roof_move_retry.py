"""Tests for blind_set_position's retry/dedup handling on slat-roof motors
(issue #7).

Slat-roof motors have no position axis and, per issue #7's logs, go back to
sleep more eagerly than the geared actuators the default blindMoveToPos retry
count (retry=3, ~2 s total) was tuned for. Two things follow from that:

1. Such motors get a longer retry budget (SLAT_ROOF_MOVE_RETRY), matching the
   ~5 s a single command gets in practice.
2. A new set_position call for the same blind must drop any blindMoveToPos
   still queued/retrying for it - otherwise clicking the tilt slider again
   before the motor answered the previous command piles up multiple,
   sometimes contradictory, targets that then fire one after another.
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

from pywarema.stick import SLAT_ROOF_MOVE_RETRY, WmsStick  # noqa: E402


def make_stick() -> WmsStick:
    return WmsStick(
        port="/dev/null",
        channel=17,
        pan_id="ABCD",
        key="0123456789ABCDEF0123456789ABCDEF",
        callback=lambda *_: None,
        auto_open=False,
    )


class TestSlatRoofMoveRetry(unittest.TestCase):
    def setUp(self):
        self.stick = make_stick()

    def test_slat_roof_motor_gets_extended_retry_budget(self):
        blind = self.stick.blind_add(1984827, "Slat roof")
        blind.product_type = 27  # SlatRoofL60
        self.stick.blind_set_position(blind.snr, position=None, angle=50)
        queued = self.stick._msg_queue[0]
        self.assertEqual(queued.cmd, "blindMoveToPos")
        self.assertEqual(queued.retry, SLAT_ROOF_MOVE_RETRY)

    def test_non_slat_roof_motor_keeps_default_retry_budget(self):
        blind = self.stick.blind_add(1234567, "Roller blind")
        blind.product_type = 1  # not a slat-roof layout
        self.stick.blind_set_position(blind.snr, position=50, angle=None)
        queued = self.stick._msg_queue[0]
        self.assertEqual(queued.cmd, "blindMoveToPos")
        self.assertNotEqual(queued.retry, SLAT_ROOF_MOVE_RETRY)

    def test_second_command_drops_stale_queued_command_for_same_blind(self):
        blind = self.stick.blind_add(1984827, "Slat roof")
        blind.product_type = 27
        self.stick.blind_set_position(blind.snr, position=None, angle=-50)
        self.stick.blind_set_position(blind.snr, position=None, angle=50)
        move_msgs = [m for m in self.stick._msg_queue if m.cmd == "blindMoveToPos"]
        self.assertEqual(len(move_msgs), 1)
        self.assertEqual(move_msgs[0].params["ang"], 50)

    def test_stale_command_for_a_different_blind_is_kept(self):
        blind_a = self.stick.blind_add(1984827, "Slat roof A")
        blind_a.product_type = 27
        blind_b = self.stick.blind_add(1111111, "Slat roof B")
        blind_b.product_type = 27
        self.stick.blind_set_position(blind_a.snr, position=None, angle=-50)
        self.stick.blind_set_position(blind_b.snr, position=None, angle=50)
        move_msgs = [m for m in self.stick._msg_queue if m.cmd == "blindMoveToPos"]
        self.assertEqual(len(move_msgs), 2)
        self.assertEqual({m.snr for m in move_msgs}, {blind_a.snr_hex, blind_b.snr_hex})


if __name__ == "__main__":
    unittest.main()
