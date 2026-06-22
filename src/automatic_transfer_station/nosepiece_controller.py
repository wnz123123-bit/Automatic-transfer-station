from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


_log = logging.getLogger("ats.objective")

DEFAULT_NOSEPIECE_BAUDRATE = 9600
# Default serial port pre-selected for the LV-NCNT2 nosepiece in the COM dialog.
DEFAULT_NOSEPIECE_PORT = "COM7"
OBJECTIVE_POSITION_BY_LABEL = {
    "5X": 1,
    "10X": 2,
    "20X": 3,
    "40X": 4,
    "50X": 5,
}

ERROR_NAMES = {
    "a": "Command error",
    "b": "Data Error",
    "d": "Control Timeout Error",
    "f": "Control Forbidden Error",
    "4": "Receive buffer overflow",
    "5": "Hardware error",
}

# Max forward (cRCW) clicks to try when stepping to a target address: 5 positions
# plus margin to reseat from an unidentified (RAR=0) address.
FORWARD_STEP_LIMIT = 8


class NosepieceError(RuntimeError):
    pass


class NosepieceCommandError(NosepieceError):
    def __init__(self, command: str, code: str, error_name: str):
        self.command = command
        self.code = code
        self.error_name = error_name
        super().__init__(f"{command} failed: {error_name}")


class NosepieceController(Protocol):
    is_open: bool

    def connect(self) -> None:
        ...

    def disconnect(self) -> None:
        ...

    def read_position(self) -> int:
        ...

    def move_to_position(self, position: int) -> None:
        ...


@dataclass(frozen=True)
class NosepieceResponse:
    kind: str
    command: str
    data: str


class SerialNosepieceController:
    # Per-read timeout (seconds). The LV-NCNT2 responds well within 1 s over USB-
    # serial; 2.0 s provides generous margin while bounding the worst-case per-
    # iteration cost of the 8-step retry loop.
    DEFAULT_TIMEOUT = 2.0
    # Total wall-clock budget (seconds) for the entire _request retry loop.
    # Even with 8 iterations × 2 s per read the theoretical max is 16 s; the
    # deadline caps the actual wall time regardless of iteration count.
    REQUEST_LOOP_DEADLINE = 12.0

    def __init__(
        self,
        port: str,
        baudrate: int = DEFAULT_NOSEPIECE_BAUDRATE,
        timeout: float = DEFAULT_TIMEOUT,
        write_timeout: float = 2.0,
        serial_factory=None,
        trace: Callable[[str], None] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.write_timeout = write_timeout
        self._serial_factory = serial_factory
        # Optional diagnostic sink. When set, every TX/RX frame is reported as a
        # readable string. Purely observational: it does not change any command,
        # parsing, or arrival-verification behaviour.
        self._trace = trace
        # Injectable clock for deterministic testing of the loop deadline.
        self._clock = clock if clock is not None else time.monotonic
        self._serial = None
        self.is_open = False
        # Serializes all serial IO (whole _request + disconnect) so a move/read
        # and a disconnect cannot interleave on the shared handle (Fix 4).
        self._io_lock = threading.Lock()
        # True only after an explicit disconnect (or a never-opened controller
        # whose handle was closed). When set, _request refuses to silently
        # re-open the port — a deliberately-disconnected link must stay closed
        # until the operator explicitly reconnects (Fix 4/5).
        self._deliberately_closed = False

    def _emit_trace(self, direction: str, raw: object) -> None:
        sink = self._trace
        if sink is None:
            return
        if isinstance(raw, (bytes, bytearray)):
            data = bytes(raw)
            if data:
                shown = data.decode("ascii", "backslashreplace").replace("\r", "\\r").replace("\n", "\\n")
                msg = f"{direction} '{shown}' hex=[{data.hex(' ')}]"
            else:
                msg = f"{direction} <empty/timeout>"
        else:
            msg = f"{direction} {raw}"
        try:
            sink(msg)
        except Exception:
            pass

    def connect(self) -> None:
        if self._serial is not None:
            return
        factory = self._serial_factory
        if factory is None:
            import serial

            factory = serial.Serial
        self._serial = factory(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=self.timeout,
            write_timeout=self.write_timeout,
        )
        self.is_open = True
        # An explicit connect clears the "stay closed" latch so the next request
        # may proceed against the freshly-opened handle.
        self._deliberately_closed = False

    def disconnect(self) -> None:
        # Take the IO lock so a disconnect cannot interleave with an in-flight
        # _request on the shared handle (Fix 4).
        with self._io_lock:
            if self._serial is not None:
                self._serial.close()
                self._serial = None
            self.is_open = False
            # Latch the deliberate close so a later read/move does not silently
            # reopen the port behind the operator's back (Fix 5).
            self._deliberately_closed = True

    def read_position(self) -> int:
        response = self._request("r", "RAR", expected_kind="a")
        if response.data not in {"0", "1", "2", "3", "4", "5"}:
            raise NosepieceError(f"invalid nosepiece address response: {response.data!r}")
        return int(response.data)

    def move_to_position(self, position: int) -> None:
        self._validate_position(position)
        # Rotate FORWARD one click at a time (cRCW) until RAR confirms the target.
        # The LV-NU5A only seats reliably when rotated forward; the direct
        # "go to address" command (cRDC) drives in the REVERSE direction and stalls
        # on the firmware's reverse 1->5 prohibition (it returns a Control Timeout
        # 'd' and leaves the turret between detents, RAR=0). Forward stepping never
        # hits that prohibition; forward order wraps 5 -> 1. See LV-NCNT2 manual,
        # "List of Control Commands" and "Method of Operation".
        if self._read_address_or_none() == position:
            return
        for _ in range(FORWARD_STEP_LIMIT):
            try:
                self._request("c", "RCW", expected_kind="o")  # one forward click; oRCW = seated
            except NosepieceCommandError as exc:
                # The LV-NCNT2 frequently returns a Control Timeout ('d') on cRCW even
                # though the turret physically advanced/seated (the control ACK times
                # out before the detent settles). Treat 'd' as "unconfirmed — verify
                # by RAR" rather than a hard failure: the RAR read below decides
                # whether we reached the target or send another forward click.
                # Any OTHER command error (command/data/forbidden/overflow/hardware)
                # is a genuine fault and propagates.
                if exc.code != "d":
                    raise
                _log.debug("nosepiece cRCW returned Control Timeout; confirming address via RAR")
                self._emit_trace("-- ", "cRCW Control Timeout swallowed; confirming via RAR")
            if self._read_address_or_none() == position:
                return
        raise NosepieceError(f"nosepiece did not reach address {position} after forward stepping")

    def _read_address_or_none(self) -> int | None:
        # Used only inside the move loop to confirm arrival. A transient Control
        # Timeout ('d') on the RAR read is treated as "address not yet identifiable"
        # (return None) so the move re-confirms / steps again instead of aborting —
        # the explicit "读取位置" path (read_position) stays strict and surfaces the
        # error. RAR=0 (unidentified) likewise maps to None.
        try:
            address = self.read_position()
        except NosepieceCommandError as exc:
            if exc.code == "d":
                return None
            raise
        return address if address in (1, 2, 3, 4, 5) else None

    def _close_serial_locked(self) -> None:
        """Close and clear the serial handle, mirroring the self-heal pattern.

        Called when a serial-layer exception is detected in _request or
        _read_response so the next _request call will re-open cleanly.
        """
        serial = self._serial
        self._serial = None
        self.is_open = False
        if serial is not None:
            try:
                serial.close()
            except Exception:
                pass

    def _request(self, kind: str, command: str, data: str = "", expected_kind: str = "o") -> NosepieceResponse:
        # Serialize the whole request under the IO lock so a concurrent disconnect
        # cannot close the handle out from under us mid-frame (Fix 4).
        with self._io_lock:
            return self._request_locked(kind, command, data, expected_kind)

    def _request_locked(self, kind: str, command: str, data: str = "", expected_kind: str = "o") -> NosepieceResponse:
        if self._deliberately_closed:
            # The port was deliberately disconnected; do NOT silently reopen it.
            # The operator must explicitly reconnect first (Fix 5).
            raise NosepieceError("nosepiece serial port is not open")
        if self._serial is None and self.is_open:
            # The handle vanished mid-operation (e.g. the port was closed out from
            # under an in-flight move/read by a concurrent disconnect) while the
            # link was still considered open. Raise rather than silently reopening,
            # so a yanked port surfaces as an error instead of masking corruption
            # of the in-flight transaction (Fix 4).
            raise NosepieceError("nosepiece serial port is not open")
        # Lazily open the port on first use. After a deliberate disconnect (latched
        # above) or a mid-op yank (caught above) this never silently reopens.
        self.connect()
        serial_port = self._serial
        if serial_port is None:
            raise NosepieceError("nosepiece serial port is not open")

        try:
            if hasattr(serial_port, "reset_input_buffer"):
                serial_port.reset_input_buffer()
            payload = f"{kind}{command}{data}\r".encode("ascii")
            self._emit_trace("TX ", payload)
            serial_port.write(payload)
            if hasattr(serial_port, "flush"):
                serial_port.flush()
        except Exception as exc:
            self._close_serial_locked()
            raise NosepieceError(f"serial write error for {kind}{command}: {exc}") from exc

        deadline = self._clock() + self.REQUEST_LOOP_DEADLINE
        for _ in range(8):
            if self._clock() > deadline:
                raise NosepieceError(
                    f"loop deadline exceeded waiting for response to {kind}{command}"
                )
            response = self._read_response()
            if response.kind == "s" and response.command == "SAE":
                continue
            if response.kind == "n":
                if response.command != command:
                    raise NosepieceError(
                        f"unexpected NAK {response.kind}{response.command}{response.data!r} for {kind}{command}"
                    )
                code = response.data[:1]
                error_name = ERROR_NAMES.get(code, f"unknown error code {code!r}")
                raise NosepieceCommandError(command, code, error_name)
            if response.kind != expected_kind or response.command != command:
                raise NosepieceError(
                    f"unexpected response {response.kind}{response.command}{response.data!r} for {kind}{command}"
                )
            return response

        raise NosepieceError(f"no command response received for {kind}{command}")

    def _read_response(self) -> NosepieceResponse:
        serial_port = self._serial
        if serial_port is None:
            raise NosepieceError("nosepiece serial port is not open")
        # size=64 is >= the longest legal LV-NCNT2 frame. read_until returns when
        # it finds the terminator OR when size bytes have been read. A full buffer
        # without a trailing \r is a framing error (garbled or oversized response).
        try:
            raw = serial_port.read_until(b"\r", size=64)
        except Exception as exc:
            self._close_serial_locked()
            raise NosepieceError(f"serial read error: {exc}") from exc
        self._emit_trace("RX ", raw)
        if not raw:
            raise NosepieceError("timeout waiting for nosepiece response")
        if raw and not raw.endswith(b"\r"):
            raise NosepieceError(
                f"framing error: nosepiece response did not end with \\r (size cap hit?): {raw!r}"
            )
        try:
            message = raw.decode("ascii").strip("\r\n")
        except UnicodeDecodeError as exc:
            raise NosepieceError(f"non-ASCII nosepiece response: {raw!r}") from exc
        if len(message) < 4:
            raise NosepieceError(f"short nosepiece response: {message!r}")
        return NosepieceResponse(kind=message[0], command=message[1:4], data=message[4:])

    @staticmethod
    def _validate_position(position: int) -> None:
        if position not in {1, 2, 3, 4, 5}:
            raise ValueError("nosepiece position must be 1..5")


class DisabledNosepieceController:
    is_open = False

    def connect(self) -> None:
        raise NosepieceError("nosepiece serial port is not configured")

    def disconnect(self) -> None:
        return None

    def read_position(self) -> int:
        raise NosepieceError("nosepiece serial port is not configured")

    def move_to_position(self, position: int) -> None:
        SerialNosepieceController._validate_position(position)
        raise NosepieceError("nosepiece serial port is not configured")
