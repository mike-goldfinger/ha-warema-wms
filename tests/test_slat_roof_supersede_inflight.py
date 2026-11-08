"""Tests for superseding an in-flight blindMoveToPos (issue #7).

blind_set_position drops blindMoveToPos messages still *queued* for the same
blind (test_slat_roof_move_retry.py). The command that is already in flight -
sent, waiting for the reply or retrying after a timeout - lives in
``_current_msg`` and was not reached by that. On a slat-roof motor, which
misses roughly a third of its frames and therefore spends most of its retry
budget, a second slider tap arrived while the first command was still
retrying; the first command was then re-sent *after* the second one, because
a retry was appended to the end of the queue. The v1.9.0-beta.5 test log
reports exactly that: "the system stopped responding and executed the stuff
later", "an intermediate stop before continuing".

Covered here:

1. A new set_position marks the in-flight blindMoveToPos for the same blind
   as superseded; its timeout then drops it instead of retrying it.
2. A retry of a priority message (user command) keeps its place at the head
   of the queue instead of queuing behind the background position polls.
3. blind_stop supersedes the in-flight move and is queued with priority.
4. A late reply to a superseded command does not update the blind state.
5. Commands for other blinds, and other command types, are left alone.
"""

import os
import sys
import types
import unittest
from unittest import mock

sys.modules.setdefault("serial", types.SimpleNamespace(Serial=object))

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "custom_components",
        "warema_wms",
    ),
)

import pywarema.stick as stick_mod  # noqa: E402
from pywarema.stick import WmsMessage, WmsStick  # noqa: E402


class _NoTimer:
    """threading.Timer stand-in that never fires.

    The queue is driven by timers; in these tests every step is taken by
    hand so the outcome does not depend on timing.
    """

    def __init__(self, *_args, **_kwargs):
        self.daemon = False

    def start(self):
        return None

    def cancel(self):
        return None


def make_stick() -> WmsStick:
    return WmsStick(
        port="/dev/null",
        channel=17,
        pan_id="ABCD",
        key="0123456789ABCDEF0123456789ABCDEF",
        callback=lambda *_: None,
        auto_open=False,
    )


class TestSupersedeInFlightMove(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(stick_mod.threading, "Timer", _NoTimer)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stick = make_stick()
        self.roof = self.stick.blind_add(1984827, "Slat roof")
        self.roof.product_type = 27  # SlatRoofL60

    def _send_next(self) -> WmsMessage:
        """Take the head of the queue 'in flight', as _process_queue would."""
        msg = self.stick._msg_queue.pop(0)
        self.stick._current_msg = msg
        return msg

    def _moves(self):
        return [m for m in self.stick._msg_queue if m.cmd == "blindMoveToPos"]

    # 1. -----------------------------------------------------------------
    def test_new_command_supersedes_in_flight_move_and_timeout_drops_it(self):
        self.stick.blind_set_position(self.roof.snr, position=None, angle=-50)
        first = self._send_next()
        self.assertFalse(first.superseded)

        self.stick.blind_set_position(self.roof.snr, position=None, angle=50)
        self.assertTrue(first.superseded)
        self.assertEqual([m.params["ang"] for m in self._moves()], [50])

        # The first command times out while the second is already queued.
        self.stick._on_timeout()
        self.assertIsNone(self.stick._current_msg)
        # Not retried: still exactly one move in the queue, the new target.
        self.assertEqual([m.params["ang"] for m in self._moves()], [50])
        self.assertNotIn(first, self.stick._msg_queue)

    def test_superseded_timeout_reports_superseded_not_timeout(self):
        seen = []
        self.stick.blind_set_position(
            self.roof.snr,
            position=None,
            angle=-50,
            on_complete=lambda err, *_: seen.append(err),
        )
        self._send_next()
        self.stick.blind_set_position(self.roof.snr, position=None, angle=50)
        self.stick._on_timeout()
        self.assertEqual(seen, ["superseded"])

    # 2. -----------------------------------------------------------------
    def test_retry_of_priority_command_stays_ahead_of_polls(self):
        other = self.stick.blind_add(1111111, "Roller blind")
        self.stick.blind_set_position(self.roof.snr, position=None, angle=50)
        move = self._send_next()
        self.assertTrue(move.priority)
        # Background polls arrive while the command waits for its reply.
        self.stick.blind_get_position(other.snr_hex, retry=1)
        self.stick.blind_get_position(self.roof.snr_hex, retry=1)
        self.assertEqual(self.stick._msg_queue[0].cmd, "blindGetPos")

        self.stick._on_timeout()
        self.assertIs(self.stick._msg_queue[0], move)
        self.assertEqual(move.retry, stick_mod.SLAT_ROOF_MOVE_RETRY - 1)

    def test_retry_of_background_poll_queues_at_the_end(self):
        other = self.stick.blind_add(1111111, "Roller blind")
        self.stick.blind_get_position(self.roof.snr_hex, retry=1)
        poll = self._send_next()
        self.assertFalse(poll.priority)
        self.stick.blind_get_position(other.snr_hex, retry=1)

        self.stick._on_timeout()
        self.assertIs(self.stick._msg_queue[-1], poll)
        self.assertEqual(self.stick._msg_queue[0].snr, other.snr_hex)

    # 3. -----------------------------------------------------------------
    def test_stop_supersedes_in_flight_move_and_goes_first(self):
        other = self.stick.blind_add(1111111, "Roller blind")
        self.stick.blind_set_position(self.roof.snr, position=None, angle=50)
        move = self._send_next()
        self.stick.blind_get_position(other.snr_hex, retry=1)

        self.stick.blind_stop(self.roof.snr, get_pos_on_stop=False)
        self.assertTrue(move.superseded)
        self.assertEqual(self.stick._msg_queue[0].cmd, "blindStopMove")
        self.assertEqual(self.stick._msg_queue[0].snr, self.roof.snr_hex)

        # The old move times out after the stop was queued: it must not come
        # back, or the motor would set off again after stopping.
        self.stick._on_timeout()
        self.assertEqual(self._moves(), [])

    # 4. -----------------------------------------------------------------
    def test_late_reply_to_superseded_move_does_not_update_state(self):
        self.stick.blind_set_position(self.roof.snr, position=None, angle=-50)
        first = self._send_next()
        self.stick.blind_set_position(self.roof.snr, position=None, angle=50)
        before = self.roof.pos_current

        # The motor answers the first command after all.
        self.stick._handle_received_msg(
            {
                "msg_type": "blindMoveToPosResponse",
                "snr": self.roof.snr_hex,
                "snr_num": self.roof.snr,
                "params": {},
            }
        )
        self.assertIsNone(self.stick._current_msg)
        self.assertEqual(self.roof.pos_current, before)

        # The reply to the live command is adopted as before.
        second = self._send_next()
        self.assertFalse(second.superseded)
        self.stick._handle_received_msg(
            {
                "msg_type": "blindMoveToPosResponse",
                "snr": self.roof.snr_hex,
                "snr_num": self.roof.snr,
                "params": {},
            }
        )
        self.assertEqual(self.roof.pos_current.ang, 50)
        self.assertTrue(self.roof.pos_current.moving)

    # 5. -----------------------------------------------------------------
    def test_in_flight_move_of_another_blind_is_not_touched(self):
        other = self.stick.blind_add(1111111, "Slat roof B")
        other.product_type = 27
        self.stick.blind_set_position(other.snr, position=None, angle=-50)
        other_move = self._send_next()

        self.stick.blind_set_position(self.roof.snr, position=None, angle=50)
        self.assertFalse(other_move.superseded)

        self.stick._on_timeout()
        self.assertIn(other_move, self.stick._msg_queue)

    def test_in_flight_poll_is_not_superseded_by_a_move(self):
        self.stick.blind_get_position(self.roof.snr_hex, retry=1)
        poll = self._send_next()
        self.stick.blind_set_position(self.roof.snr, position=None, angle=50)
        self.assertFalse(poll.superseded)


if __name__ == "__main__":
    unittest.main()
