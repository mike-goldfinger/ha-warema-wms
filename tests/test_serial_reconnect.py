"""Tests for the serial link failure paths (USB stick or socket:// via ser2net).

pyserial is supplied by Home Assistant in production.  Stub it here like the
other protocol tests so this stays runnable with the standard library alone.
"""

import os
import sys
import types
import unittest
from unittest.mock import patch

sys.modules.setdefault("serial", types.SimpleNamespace(Serial=object))
_serial_stub = sys.modules["serial"]
if not hasattr(_serial_stub, "SerialException"):
    _serial_stub.SerialException = type("SerialException", (IOError,), {})

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "custom_components",
        "warema_wms",
    ),
)

from pywarema import stick as stick_module  # noqa: E402
from pywarema.stick import WmsMessage, WmsStick  # noqa: E402

SerialException = stick_module.serial.SerialException


class FakeSerial:
    """Minimal serial port double."""

    def __init__(self, read_exc=None, write_exc=None):
        self.is_open = True
        self.read_exc = read_exc
        self.write_exc = write_exc
        self.written = []

    def read(self, _size):
        if self.read_exc is not None:
            raise self.read_exc
        return b""

    def write(self, data):
        if self.write_exc is not None:
            raise self.write_exc
        self.written.append(data)

    def close(self):
        self.is_open = False


class TimerSpy:
    """Record timers without starting background threads."""

    instances = []

    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.__class__.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        pass


def _message(cmd="blindGetPos"):
    return WmsMessage(cmd, "000001", {})


class TestOpenSerial(unittest.TestCase):
    """The port must be passed positionally: serial_for_url(url, ...)."""

    def test_serial_for_url_gets_port_positionally(self):
        stick = WmsStick(
            port="socket://192.168.1.2:8638",
            channel=17,
            pan_id="ABCD",
            key="0123456789ABCDEF0123456789ABCDEF",
            callback=lambda *_: None,
            auto_open=False,
        )
        calls = []

        def fake_serial_for_url(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeSerial()

        with patch.multiple(
            stick_module.serial,
            create=True,
            serial_for_url=fake_serial_for_url,
            EIGHTBITS=8,
            PARITY_NONE="N",
            STOPBITS_ONE=1,
        ):
            stick._open_serial()

        self.assertEqual(len(calls), 1)
        args, kwargs = calls[0]
        self.assertEqual(args, ("socket://192.168.1.2:8638",))
        self.assertNotIn("port", kwargs)
        self.assertEqual(kwargs["write_timeout"], 1.0)


class TestSerialFailures(unittest.TestCase):
    """A dead link must trigger a reconnect from both the read and write side."""

    def setUp(self):
        TimerSpy.instances.clear()
        self.stick = WmsStick(
            port="socket://192.168.1.2:8638",
            channel=17,
            pan_id="ABCD",
            key="0123456789ABCDEF0123456789ABCDEF",
            callback=lambda *_: None,
            auto_open=False,
        )

    def test_write_failure_keeps_message_and_requests_reconnect(self):
        self.stick._serial = FakeSerial(
            write_exc=SerialException("write failed: Broken pipe")
        )
        msg = _message()
        self.stick._msg_queue.append(msg)

        with patch("pywarema.stick.threading.Timer", TimerSpy):
            self.stick._process_queue()

        self.assertIsNone(self.stick._current_msg)
        self.assertEqual(self.stick._msg_queue, [msg])
        self.assertTrue(self.stick._reconnect_event.is_set())
        self.assertEqual(TimerSpy.instances, [])

    def test_queue_waits_while_reconnect_pending(self):
        self.stick._serial = FakeSerial()
        self.stick._msg_queue.append(_message())
        self.stick._reconnect_event.set()

        with patch("pywarema.stick.threading.Timer", TimerSpy):
            self.stick._process_queue()

        self.assertIsNone(self.stick._current_msg)
        self.assertEqual(len(self.stick._msg_queue), 1)
        self.assertEqual(self.stick._serial.written, [])

    def test_reader_reconnects_on_socket_disconnect(self):
        """Any read error (not only the USB text) must try to reconnect."""
        self.stick._serial = FakeSerial(read_exc=SerialException("socket disconnected"))
        attempts = []

        def fake_reconnect():
            attempts.append(1)
            self.stick._stop_event.set()
            return True

        with patch.object(self.stick, "_reconnect_serial", fake_reconnect):
            self.stick._reader_loop()

        self.assertEqual(attempts, [1])
        self.assertNotEqual(self.stick.status, "error")

    def test_reader_stops_when_reconnect_fails(self):
        self.stick._serial = FakeSerial(
            read_exc=SerialException("read failed: timed out")
        )

        with patch.object(self.stick, "_reconnect_serial", return_value=False):
            self.stick._reader_loop()  # must return instead of spinning

    def test_reader_handles_reconnect_request_from_writer(self):
        self.stick._serial = FakeSerial()
        self.stick._reconnect_event.set()
        attempts = []

        def fake_reconnect():
            attempts.append(1)
            self.stick._stop_event.set()
            return True

        with patch.object(self.stick, "_reconnect_serial", fake_reconnect):
            self.stick._reader_loop()

        self.assertEqual(attempts, [1])

    def test_reconnect_success_resumes_queue(self):
        self.stick._serial = FakeSerial()
        self.stick._reconnect_event.set()
        self.stick._recv_buffer = "{garbage"

        with patch("pywarema.stick.threading.Timer", TimerSpy), patch.object(
            self.stick, "_open_serial", return_value=FakeSerial()
        ), patch("pywarema.stick.time.sleep"):
            self.assertTrue(self.stick._reconnect_serial())

        self.assertFalse(self.stick._reconnecting)
        self.assertFalse(self.stick._reconnect_event.is_set())
        self.assertEqual(self.stick._recv_buffer, "")
        self.assertEqual(len(TimerSpy.instances), 1)
        self.assertEqual(
            TimerSpy.instances[0].callback.__func__, self.stick._process_queue.__func__
        )

    def test_reconnect_gives_up_after_retries(self):
        self.stick._serial = FakeSerial()

        with patch.object(
            self.stick,
            "_open_serial",
            side_effect=SerialException("Could not open port"),
        ) as opener, patch("pywarema.stick.time.sleep"):
            self.assertFalse(self.stick._reconnect_serial())

        self.assertEqual(opener.call_count, 5)
        self.assertEqual(self.stick.status, "error")
        self.assertFalse(self.stick._reconnecting)


if __name__ == "__main__":
    unittest.main()
