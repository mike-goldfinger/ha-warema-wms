"""Tests for per-device key/transmission mode ("{R<key><transmission>").

WMS Studio Pro addresses networked devices with the network key (Key1 + P2P,
"R11") and falls back to the factory key on the broadcast PAN for devices that
never joined. Radio motors also answer the factory key on the network PAN
("R06"), dimming actuators apparently do not. See issue #8.
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

from pywarema.stick import (  # noqa: E402
    BROADCAST_PAN_ID,
    DEFAULT_RADIO_MODE,
    WmsMessage,
    WmsStick,
)

SNR = 1688396


def make_stick() -> WmsStick:
    return WmsStick(
        port="/dev/null",
        channel=17,
        pan_id="ABCD",
        key="0123456789ABCDEF0123456789ABCDEF",
        callback=lambda *_: None,
        auto_open=False,
    )


class TestFrameAddressing(unittest.TestCase):
    def setUp(self):
        self.stick = make_stick()
        self.blind = self.stick.blind_add(SNR, "light")

    def _enqueued_frame(self, msg: WmsMessage) -> str:
        self.stick._enqueue(msg)
        return self.stick._msg_queue[-1].stick_cmd["cmd"]

    def test_default_mode_keeps_frame(self):
        frame = self._enqueued_frame(WmsMessage("lightSetLevel", SNR, {"level": 50}))
        self.assertTrue(frame.startswith("{R06" + self.blind.snr_hex))

    def test_device_mode_rewrites_unicast_frames(self):
        self.blind.radio_mode = "11"
        for cmd, params in (
            ("lightSetLevel", {"level": 50}),
            ("blindGetPos", {}),
            ("mb8Read", {"block": 81, "addr": 24, "size": 32}),
        ):
            frame = self._enqueued_frame(WmsMessage(cmd, SNR, params))
            self.assertTrue(frame.startswith("{R11" + self.blind.snr_hex), frame)

    def test_other_devices_and_stick_commands_untouched(self):
        self.blind.radio_mode = "11"
        other = self.stick.blind_add(1910873, "motor")
        frame = self._enqueued_frame(WmsMessage("blindGetPos", other.snr, {}))
        self.assertTrue(frame.startswith("{R06"))
        frame = self._enqueued_frame(WmsMessage("scanRequest", 0, {"pan_id": "ABCD"}))
        self.assertTrue(frame.startswith("{R04FFFFFF"))


class TestProbeRadioMode(unittest.TestCase):
    def setUp(self):
        self.stick = make_stick()
        self.blind = self.stick.blind_add(SNR, "probe")
        self.pan_switches: list[str] = []

    def _probe(self, answering: set[tuple[str, str]]):
        """Run the probe against a device answering the given (pan, mode) pairs."""
        current_pan = {"pan": self.stick.pan_id}

        def fake_read(snr_hex, block, addr, size, timeout=3.0):
            hit = (current_pan["pan"], self.blind.radio_mode) in answering
            return b"\x01" if hit else None

        def fake_switch(pan_id, timeout=3.0):
            self.pan_switches.append(pan_id)
            current_pan["pan"] = pan_id

        with patch.object(self.stick, "_mb8_read_sync", side_effect=fake_read):
            with patch.object(self.stick, "_switch_pan_sync", side_effect=fake_switch):
                return self.stick.probe_radio_mode(SNR)

    def test_network_key_device(self):
        self.assertEqual(self._probe({("ABCD", "11")}), ("11", "ABCD"))
        self.assertEqual(self.blind.radio_mode, "11")
        self.assertEqual(self.pan_switches, [])

    def test_unjoined_device_reported_and_pan_restored(self):
        result = self._probe({(BROADCAST_PAN_ID, "01")})
        self.assertEqual(result, ("01", BROADCAST_PAN_ID))
        self.assertEqual(self.pan_switches, [BROADCAST_PAN_ID, "ABCD"])
        self.assertEqual(self.blind.radio_mode, DEFAULT_RADIO_MODE)

    def test_silent_device(self):
        self.assertEqual(self._probe(set()), (None, None))
        self.assertEqual(self.pan_switches[-1], "ABCD")
        self.assertEqual(self.blind.radio_mode, DEFAULT_RADIO_MODE)


if __name__ == "__main__":
    unittest.main()
