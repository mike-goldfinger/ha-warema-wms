"""Tests for starting a WMS network scan from an idle command queue.

The production integration supplies pyserial through Home Assistant.  Stub it
here so this focused queue test stays runnable with the Python standard library
alone, like the other protocol tests in this directory.
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

from pywarema.stick import DELAY_MSG_PROC, WmsStick  # noqa: E402


class TimerSpy:
    """Record timers without starting background threads in a unit test."""

    instances = []

    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.started = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True


class TestScanQueue(unittest.TestCase):
    """A scan must wake queue processing when no other command is running."""

    def setUp(self):
        TimerSpy.instances.clear()
        self.stick = WmsStick(
            port="/dev/null",
            channel=17,
            pan_id="ABCD",
            key="0123456789ABCDEF0123456789ABCDEF",
            callback=lambda *_: None,
            auto_open=False,
        )

    def test_scan_starts_queue_processor(self):
        with patch("pywarema.stick.threading.Timer", TimerSpy):
            self.stick.scan_devices()

        self.assertEqual(
            [message.cmd for message in self.stick._msg_queue],
            ["scanRequest", "scanRequest", "scanRequest"],
        )
        self.assertEqual(len(TimerSpy.instances), 1)
        timer = TimerSpy.instances[0]
        self.assertEqual(timer.delay, DELAY_MSG_PROC)
        self.assertTrue(timer.started)
        self.assertEqual(timer.callback.__self__, self.stick)
        self.assertEqual(timer.callback.__func__, self.stick._process_queue.__func__)


if __name__ == "__main__":
    unittest.main()
