import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
PYTHON_DIR = SRC_DIR / "automatic_transfer_station"
sys.path.insert(0, str(SRC_DIR))


class FakeSerialPort:
    def __init__(self, responses):
        self.responses = [response if isinstance(response, bytes) else response.encode("ascii") for response in responses]
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def read_until(self, terminator=b"\r", size=None):
        if not self.responses:
            return b""
        data = self.responses.pop(0)
        # If size is specified, honour the cap: return only 'size' bytes, which
        # may not include the terminator (simulating the real pyserial behaviour
        # where read_until stops at the terminator OR at size bytes).
        if size is not None and len(data) > size:
            return data[:size]
        return data

    def reset_input_buffer(self):
        pass

    def close(self):
        self.closed = True


class NosepieceControllerTests(unittest.TestCase):
    def test_read_position_sends_rar_and_parses_acknowledged_address(self):
        from automatic_transfer_station.nosepiece_controller import SerialNosepieceController

        fake = FakeSerialPort([b"aRAR3\r"])
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)

        self.assertEqual(3, controller.read_position())
        self.assertEqual([b"rRAR\r"], fake.writes)

    def test_move_steps_forward_with_rcw_and_confirms_each_step_via_rar(self):
        from automatic_transfer_station.nosepiece_controller import SerialNosepieceController

        # 3 -> 5 is two forward clicks; RAR confirms the target after each RCW.
        fake = FakeSerialPort([b"aRAR3\r", b"oRCW\r", b"aRAR4\r", b"oRCW\r", b"aRAR5\r"])
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)

        controller.move_to_position(5)

        self.assertEqual([b"rRAR\r", b"cRCW\r", b"rRAR\r", b"cRCW\r", b"rRAR\r"], fake.writes)

    def test_move_to_current_position_sends_no_rotation(self):
        from automatic_transfer_station.nosepiece_controller import SerialNosepieceController

        fake = FakeSerialPort([b"aRAR4\r"])  # already at 4
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)

        controller.move_to_position(4)

        self.assertEqual([b"rRAR\r"], fake.writes)  # no cRCW issued

    def test_move_reseats_from_unidentified_address_then_steps_forward(self):
        from automatic_transfer_station.nosepiece_controller import SerialNosepieceController

        # RAR=0 (address unidentified); forward clicks reseat to 1 then step to 2.
        fake = FakeSerialPort([b"aRAR0\r", b"oRCW\r", b"aRAR1\r", b"oRCW\r", b"aRAR2\r"])
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)

        controller.move_to_position(2)

        self.assertEqual([b"rRAR\r", b"cRCW\r", b"rRAR\r", b"cRCW\r", b"rRAR\r"], fake.writes)

    def test_move_to_position_rejects_invalid_address_before_serial_write(self):
        from automatic_transfer_station.nosepiece_controller import SerialNosepieceController

        fake = FakeSerialPort([b"aRAR1\r"])
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)

        with self.assertRaises(ValueError):
            controller.move_to_position(6)

        self.assertEqual([], fake.writes)

    def test_forward_step_nak_raises_with_code_name(self):
        from automatic_transfer_station.nosepiece_controller import NosepieceError, SerialNosepieceController

        # current 3, then the forward click is NAK'd (hardware error code '5').
        fake = FakeSerialPort([b"aRAR3\r", b"nRCW5\r"])
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)

        with self.assertRaisesRegex(NosepieceError, "Hardware error"):
            controller.move_to_position(5)

    def test_move_raises_when_target_never_confirmed(self):
        from automatic_transfer_station.nosepiece_controller import (
            FORWARD_STEP_LIMIT,
            NosepieceError,
            SerialNosepieceController,
        )

        # Forward clicks "succeed" but RAR never reaches the target -> strict raise.
        fake = FakeSerialPort([b"aRAR0\r"] + [b"oRCW\r", b"aRAR0\r"] * FORWARD_STEP_LIMIT)
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)

        with self.assertRaises(NosepieceError):
            controller.move_to_position(1)

    def test_status_messages_are_ignored_until_command_response_arrives(self):
        from automatic_transfer_station.nosepiece_controller import SerialNosepieceController

        fake = FakeSerialPort([b"sSAE0\r", b"sSAE5\r", b"aRAR5\r"])
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)

        self.assertEqual(5, controller.read_position())

    # ---- Fix 6: bounded total time for the step-retry loop ----

    def test_request_loop_aborts_when_wall_clock_deadline_is_exceeded(self):
        """When the injectable clock says the deadline has passed, _request must
        raise NosepieceError with a deadline-exceeded message rather than spinning
        through all 8 iterations. Uses a fake clock — no real sleeps."""
        from automatic_transfer_station.nosepiece_controller import NosepieceError, SerialNosepieceController

        # Provide enough SAE (status) messages to consume all 8 iterations if
        # the deadline check were absent.
        fake = FakeSerialPort([b"sSAE0\r"] * 10)

        # A clock that starts just before the deadline and immediately crosses it.
        times = iter([0.0, 0.0, SerialNosepieceController.REQUEST_LOOP_DEADLINE + 0.1])

        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake, clock=lambda: next(times))

        with self.assertRaisesRegex(NosepieceError, "deadline"):
            controller._request("r", "RAR", expected_kind="a")

        # Fewer than 8 read_until calls should have been needed.
        self.assertLess(len(fake.writes), 2)  # only 1 TX was issued

    def test_request_loop_succeeds_on_happy_path_with_fake_clock(self):
        """With a fake clock that stays well before the deadline, the happy path
        (single SAE then the real response) must work correctly."""
        from automatic_transfer_station.nosepiece_controller import SerialNosepieceController

        fake = FakeSerialPort([b"sSAE0\r", b"aRAR3\r"])
        clock_value = [0.0]  # stays well within deadline
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake, clock=lambda: clock_value[0])

        result = controller._request("r", "RAR", expected_kind="a")

        self.assertEqual("a", result.kind)
        self.assertEqual("RAR", result.command)
        self.assertEqual("3", result.data)

    # ---- Fix 5: read_until size cap — framing error on non-terminated response ----

    def test_read_response_raises_framing_error_when_no_terminator_within_size(self):
        """If read_until returns data that does not end with \\r (size cap hit),
        _read_response must raise NosepieceError, not return a garbled response."""
        from automatic_transfer_station.nosepiece_controller import NosepieceError, SerialNosepieceController

        # 65 bytes of 'X' — exceeds size=64 so read_until truncates to 64 bytes, none is \r.
        oversized = b"X" * 65
        fake = FakeSerialPort([oversized])
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)
        # Inject the fake serial directly so _read_response can be called in isolation
        # without going through _request (which would consume it in the request loop).
        controller._serial = fake

        with self.assertRaisesRegex(NosepieceError, "framing error"):
            controller._read_response()

    def test_read_response_accepts_normal_response_ending_with_cr(self):
        """A normal response ending with \\r must still be parsed correctly."""
        from automatic_transfer_station.nosepiece_controller import SerialNosepieceController

        fake = FakeSerialPort([b"aRAR3\r"])
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)
        # Inject the fake serial directly so _read_response can be called in isolation.
        controller._serial = fake

        response = controller._read_response()

        self.assertEqual("a", response.kind)
        self.assertEqual("RAR", response.command)
        self.assertEqual("3", response.data)

    def test_trace_records_forward_step_dialogue(self):
        from automatic_transfer_station.nosepiece_controller import SerialNosepieceController

        fake = FakeSerialPort([b"aRAR3\r", b"oRCW\r", b"aRAR4\r"])
        log = []
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake, trace=log.append)

        controller.move_to_position(4)

        joined = "\n".join(log)
        self.assertIn("TX  'rRAR\\r'", joined)
        self.assertIn("RX  'aRAR3\\r'", joined)
        self.assertIn("TX  'cRCW\\r'", joined)
        self.assertIn("RX  'oRCW\\r'", joined)
        self.assertIn("hex=[", joined)  # byte-level detail for protocol diagnosis

    # ---- Fix 4: serialize serial IO; no silent reopen mid-operation ----

    def test_request_serializes_with_io_lock(self):
        """_request must acquire the controller's IO lock so a disconnect cannot
        interleave on the shared handle mid-request."""
        import threading

        from automatic_transfer_station.nosepiece_controller import SerialNosepieceController

        fake = FakeSerialPort([b"aRAR3\r"])
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)
        self.assertIsInstance(controller._io_lock, type(threading.Lock()))

        observed = {}

        original_write = fake.write

        def write_and_check(data):
            # While inside a request the IO lock must be held (cannot be acquired).
            observed["locked_during_write"] = not controller._io_lock.acquire(blocking=False)
            if not observed["locked_during_write"]:
                controller._io_lock.release()
            return original_write(data)

        fake.write = write_and_check
        controller.read_position()
        self.assertTrue(observed["locked_during_write"])

    def test_request_raises_when_serial_is_none_and_deliberately_disconnected(self):
        """After an explicit disconnect, _request must raise rather than silently
        reopening the port via connect()."""
        from automatic_transfer_station.nosepiece_controller import NosepieceError, SerialNosepieceController

        opened = []

        def factory(**kwargs):
            fake = FakeSerialPort([b"aRAR3\r"])
            opened.append(fake)
            return fake

        controller = SerialNosepieceController("COM7", serial_factory=factory)
        controller.connect()
        self.assertEqual(1, len(opened))
        controller.disconnect()

        with self.assertRaises(NosepieceError):
            controller.read_position()
        # No second port was opened — the deliberate disconnect was honoured.
        self.assertEqual(1, len(opened))

    def test_move_to_position_raises_if_serial_disconnected_mid_operation(self):
        """If the handle is closed out from under an in-flight move, the next
        serial access must raise instead of silently reopening."""
        from automatic_transfer_station.nosepiece_controller import NosepieceError, SerialNosepieceController

        opened = []

        def factory(**kwargs):
            fake = FakeSerialPort([b"aRAR3\r"])
            opened.append(fake)
            return fake

        controller = SerialNosepieceController("COM7", serial_factory=factory)
        controller.connect()
        # Simulate the port being yanked mid-operation (e.g. concurrent disconnect).
        controller._serial = None
        controller.is_open = True  # stale flag; disconnect already happened underneath

        with self.assertRaises(NosepieceError):
            controller.move_to_position(5)
        self.assertEqual(1, len(opened))

    def test_self_heal_reopens_after_serial_error_not_blocked_by_no_reopen_guard(self):
        """The BP4 self-heal (a serial-layer error closes the handle so the next
        request re-opens cleanly) must still work — the no-silent-reopen guard
        only blocks reopen after a *deliberate* disconnect, not after self-heal."""
        from automatic_transfer_station.nosepiece_controller import SerialNosepieceController

        class FailingThenOkPort(FakeSerialPort):
            def __init__(self):
                super().__init__([b"aRAR3\r"])
                self.fail_next_write = True

            def write(self, data):
                if self.fail_next_write:
                    self.fail_next_write = False
                    raise OSError("device disconnected")
                return super().write(data)

        opened = []

        def factory(**kwargs):
            port = FailingThenOkPort() if not opened else FakeSerialPort([b"aRAR3\r"])
            opened.append(port)
            return port

        controller = SerialNosepieceController("COM7", serial_factory=factory)

        # First request hits a write error -> _close_serial_locked clears handle.
        with self.assertRaises(Exception):
            controller.read_position()
        # Second request must transparently re-open (self-heal), NOT raise.
        self.assertEqual(3, controller.read_position())
        self.assertEqual(2, len(opened))

    def test_disconnect_serializes_with_io_lock(self):
        """disconnect must take the same IO lock so it cannot interleave with an
        in-flight request on the shared handle."""
        import threading

        from automatic_transfer_station.nosepiece_controller import SerialNosepieceController

        fake = FakeSerialPort([b"aRAR3\r"])
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)
        controller.connect()

        observed = {}
        original_close = fake.close

        def close_and_check():
            observed["locked_during_close"] = not controller._io_lock.acquire(blocking=False)
            if not observed["locked_during_close"]:
                controller._io_lock.release()
            return original_close()

        fake.close = close_and_check
        controller.disconnect()
        self.assertTrue(observed["locked_during_close"])

    # ---- Control-Timeout tolerance: turret seats but firmware NAKs with 'd' ----

    def test_move_succeeds_when_cRCW_control_timeout_but_rar_confirms_target(self):
        from automatic_transfer_station.nosepiece_controller import SerialNosepieceController

        # At 4, target 5. cRCW returns a Control Timeout NAK (nRCWd) even though the
        # turret physically seated; the RAR read then confirms 5 -> move succeeds.
        fake = FakeSerialPort([b"aRAR4\r", b"nRCWd\r", b"aRAR5\r"])
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)

        controller.move_to_position(5)  # must NOT raise

        self.assertEqual([b"rRAR\r", b"cRCW\r", b"rRAR\r"], fake.writes)

    def test_move_propagates_non_timeout_command_error(self):
        from automatic_transfer_station.nosepiece_controller import (
            NosepieceCommandError,
            SerialNosepieceController,
        )

        # A Data Error ('b') on cRCW is a genuine fault and must still propagate.
        fake = FakeSerialPort([b"aRAR4\r", b"nRCWb\r"])
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)

        with self.assertRaises(NosepieceCommandError):
            controller.move_to_position(5)

    def test_move_tolerates_rar_control_timeout_and_resteps(self):
        from automatic_transfer_station.nosepiece_controller import SerialNosepieceController

        # 4 -> 5: the RAR read after the first cRCW times out ('d', treated as
        # unidentified), so the loop sends a second cRCW; the next RAR confirms 5.
        fake = FakeSerialPort([b"aRAR4\r", b"oRCW\r", b"nRARd\r", b"oRCW\r", b"aRAR5\r"])
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)

        controller.move_to_position(5)

        self.assertEqual([b"rRAR\r", b"cRCW\r", b"rRAR\r", b"cRCW\r", b"rRAR\r"], fake.writes)

    def test_explicit_read_position_still_surfaces_control_timeout(self):
        from automatic_transfer_station.nosepiece_controller import (
            NosepieceCommandError,
            SerialNosepieceController,
        )

        # The explicit "读取位置" path stays strict — a 'd' timeout is reported.
        fake = FakeSerialPort([b"nRARd\r"])
        controller = SerialNosepieceController("COM7", serial_factory=lambda **kwargs: fake)
        with self.assertRaises(NosepieceCommandError):
            controller.read_position()


if __name__ == "__main__":
    unittest.main()
