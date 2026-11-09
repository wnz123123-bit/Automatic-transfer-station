"""Worker-thread API for the matching ATS laser firmware; no automatic heartbeat.

Comp HIGH at firmware BOOT is a manufacturer startup requirement, not a safe-off
claim. Opening/closing serial can reset an Uno and reassert HIGH. An independently
qualified physical inhibit is mandatory; the shipped firmware advertises neither
interlock nor motion-trigger wiring and cannot ARM/FOLLOW/FIRE.

Only keepalive() renews an existing emission lease. Motion code must call it less
than 100 ms apart, coupled to successful motion polling (never from an unrelated
timer). The 500 ms firmware watchdog requires bench qualification; it is not a
guaranteed optical shutdown time. FIRE milliseconds describe an exposure envelope,
not a single picosecond pulse or the laser's pulse repetition frequency.

OFF/ESTOP bypass the normal request lock and cancel outstanding work. Automated
callers must pass expected_stop_generation to ARM/FOLLOW/FIRE/KEEP: a new command
issued by an old job is otherwise indistinguishable from an intentional new ARM.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import re
import threading
import time
from uuid import uuid4


FIRMWARE_VERSION = "ATS-LASER-2.1"
MAX_WINDOW_MS = 60_000
WATCHDOG_MS = 500
_ACTIVE = frozenset(("ARMED", "FOLLOW", "FIRING"))
_READY = _ACTIVE | {"DISARMED"}


class LaserError(RuntimeError):
    """Unconfirmed communication, rejected operation, or unavailable capability."""


class LaserProtocolError(LaserError):
    pass


class LaserTimeoutError(LaserError):
    pass


class LaserCancelledError(LaserError):
    pass


class LaserState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    BOOT_HOLD = "BOOT_HOLD"
    DISARMED = "DISARMED"
    ARMED = "ARMED"
    FOLLOW = "FOLLOW"
    FIRING = "FIRING"
    FAULT = "FAULT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class LaserFireToken:
    """Identity allocated atomically with the actual FIRE command sequence."""

    fire_sequence: int
    started_generation: int
    boot_nonce: str
    session_nonce: str


@dataclass(frozen=True, slots=True)
class LaserFireCompletion:
    """Firmware-attributed natural envelope expiry, never an OFF/fault receipt."""

    fire_sequence: int
    started_generation: int
    boot_nonce: str
    session_nonce: str
    completed_generation: int

    @property
    def token(self) -> LaserFireToken:
        return LaserFireToken(self.fire_sequence, self.started_generation, self.boot_nonce, self.session_nonce)


@dataclass(frozen=True, slots=True)
class LaserStatus:
    state: LaserState = LaserState.DISCONNECTED
    firmware_version: str | None = None
    interlock_wired: bool = False
    trigger_wired: bool = False
    interlock_ok: bool = False
    trigger_active: bool = False
    comp_high: bool | None = None
    last_seen_monotonic: float = 0.0
    fault: str | None = None
    watchdog_ms: int | None = None
    startup_checked: bool = False
    boot_nonce: str | None = None
    session_nonce: str | None = None
    sequence: int = 0
    report_id: int = 0
    window_remaining_ms: int = 0
    max_window_ms: int | None = None
    stop_generation: int = 0
    fire_sequence: int = 0
    completion_reason: str = "NONE"
    fire_token: LaserFireToken | None = None


@dataclass
class _Pending:
    command: str
    event: threading.Event
    status: LaserStatus | None = None
    error: BaseException | None = None
    fire_token: LaserFireToken | None = None


def _uint(text: str, maximum: int = 0xFFFFFFFF) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]{0,9}", text):
        raise LaserProtocolError("malformed unsigned protocol integer")
    value = int(text)
    if value > maximum:
        raise LaserProtocolError("protocol integer out of range")
    return value


def _bit(text: str) -> bool:
    if text not in ("0", "1"):
        raise LaserProtocolError("malformed protocol boolean")
    return text == "1"


class LaserController:
    """All calls are bounded blocking WORKER calls; status/connected are snapshots.

    serial_factory receives pyserial-compatible keyword arguments: port,
    baudrate, timeout, write_timeout. It must honor both timeouts. The only
    background thread reads replies and detects faults; it NEVER sends KEEP.
    """

    ACK_TIMEOUT_S = 0.25
    CONNECT_TIMEOUT_S = 2.0
    READ_TIMEOUT_S = 0.02
    WRITE_TIMEOUT_S = 0.10
    STATUS_MAX_AGE_S = 0.50
    MAX_FRAME_BYTES = 256

    def __init__(self, serial_factory=None):
        self._serial_factory = serial_factory
        self._serial = None
        self._lock = threading.RLock()
        self._normal_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._status = LaserStatus()
        self._connected = False
        self._closing = False
        self._reader_stop = threading.Event()
        self._boot_seen = threading.Event()
        self._reader = None
        self._pending: dict[int, _Pending] = {}
        self._cancelled: set[int] = set()
        self._session = ""
        self._boot: str | None = None
        self._sequence = 0
        self._last_report = 0
        self._last_received_sequence = 0
        self._generation = 0
        self._link_fault: str | None = None
        self._fire_token: LaserFireToken | None = None
        self._fire_completion: LaserFireCompletion | None = None

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected and self._serial is not None and not self._closing

    @property
    def has_serial_resource(self) -> bool:
        """Nonblocking UI hint: cleanup is required even after protocol failure.

        The handle stays owned until both close and reader termination succeed.
        Connection operations still enforce ownership under the lifecycle lock.
        """
        return self._serial is not None

    @property
    def status(self) -> LaserStatus:
        with self._lock:
            status = self._status
            if (self._connected and status.last_seen_monotonic
                    and time.monotonic() - status.last_seen_monotonic > self.STATUS_MAX_AGE_S):
                return replace(status, state=LaserState.UNKNOWN, comp_high=None, fault="STALE_STATUS")
            return status

    def connect(self, port: str) -> LaserStatus:
        """Handshake only with this firmware; preserve its BOOT_HOLD startup HIGH.

        No port discovery or implicit fallback to legacy commands. A failed
        connection attempts bare ESTOP and closes, but cannot confirm optical OFF.
        """
        if not isinstance(port, str) or not port.strip():
            raise LaserError("an explicit serial port is required")
        if not self._lifecycle_lock.acquire(timeout=self.ACK_TIMEOUT_S):
            raise LaserError("another connection operation is in progress")
        try:
            with self._lock:
                if self._serial is not None:
                    raise LaserError("disconnect the existing serial session first")
                self._generation += 1
                self._session, self._boot = uuid4().hex, None
                self._sequence, self._last_report = 0, 0
                self._last_received_sequence = 0
                self._pending.clear()
                self._cancelled.clear()
                self._link_fault = None
                self._clear_fire()
                self._closing = False
                self._reader_stop = threading.Event()
                self._boot_seen = threading.Event()
                self._status = LaserStatus(state=LaserState.UNKNOWN, stop_generation=self._generation)
            factory = self._serial_factory
            if factory is None:
                import serial  # Existing project dependency; imported only on explicit connect.
                factory = serial.Serial
            try:
                serial_port = factory(port=port, baudrate=115200, timeout=self.READ_TIMEOUT_S,
                                      write_timeout=self.WRITE_TIMEOUT_S)
                with self._lock:
                    self._serial = serial_port
                self._reader = threading.Thread(target=self._read_loop, args=(serial_port, self._reader_stop),
                                                name="ATSLaserReader", daemon=True)
                self._reader.start()
                # Uno DTR may invoke its bootloader. A running board need not
                # emit another BOOT banner, so absence alone is not identity.
                self._boot_seen.wait(self.CONNECT_TIMEOUT_S)
                with self._lock:
                    if self._link_fault:
                        raise LaserProtocolError(self._link_fault)
                status = self._request("HELLO", handshake=True)
                with self._lock:
                    self._connected = True
                return status
            except Exception as exc:
                self._fault_link(f"CONNECT_FAILED: {exc}")
                self._close_serial()
                if isinstance(exc, LaserError):
                    raise
                raise LaserError(f"laser connection failed: {exc}") from exc
        finally:
            self._lifecycle_lock.release()

    def disconnect(self) -> LaserStatus:
        """Attempt matched OFF, then close even on failure; raise if unconfirmed.

        A successful ACK confirms LOW BEFORE close only. DTR/reset on close can
        assert startup HIGH, so the returned disconnected comp_high is unknown.
        """
        if not self._lifecycle_lock.acquire(timeout=self.ACK_TIMEOUT_S):
            raise LaserError("another connection operation is in progress")
        try:
            with self._lock:
                if self._serial is None:
                    return self._status
                self._closing = True
            error = None
            try:
                self.off()
            except Exception as exc:
                error = exc
            finally:
                self._close_serial()
            with self._lock:
                self._status = replace(self._status, state=LaserState.DISCONNECTED,
                                       comp_high=None, startup_checked=False,
                                       fault="OFF_UNCONFIRMED" if error else None)
                result = self._status
            if error is not None:
                raise LaserError(f"disconnect OFF unconfirmed; reset may assert Comp HIGH: {error}") from error
            return result
        finally:
            self._lifecycle_lock.release()

    def off(self) -> LaserStatus:
        """Priority LOW and latched DISARMED; cancel pending normal requests."""
        return self._request("OFF", urgent=True)

    def emergency_off(self) -> LaserStatus:
        """Priority LOW and FAULT/ESTOP; explicit OFF + startup check + ARM recover."""
        return self._request("ESTOP", urgent=True)

    def confirm_startup_check(self) -> LaserStatus:
        """Human attestation only, after observing manufacturer startup self-check.

        Call OFF first. This cannot measure self-check success or replace an
        independent inhibit. Confirmation is lost on reboot/new session/fault.
        """
        return self._request("CHECK", allowed={LaserState.DISARMED}, ready=True)

    def arm(self, *, expected_stop_generation=None) -> LaserStatus:
        return self._request("ARM", allowed={LaserState.DISARMED}, ready=True,
                             startup=True, expected_generation=expected_stop_generation)

    def follow(self, window_ms, *, expected_stop_generation=None) -> LaserStatus:
        """Enter board-triggered mode only from an inactive trigger baseline.

        The cached input is a host-side rejection check; firmware must re-read
        the real pin when accepting FOLLOW to cover changes after this snapshot.
        """
        return self._request("FOLLOW", self._duration(window_ms), allowed={LaserState.ARMED},
                             ready=True, trigger=True, startup=True,
                             expected_generation=expected_stop_generation)

    def fire(self, duration_ms, *, expected_stop_generation=None) -> LaserStatus:
        """Bounded HIGH envelope; returned fire_token identifies this actual FIRE.

        A naturally completed short envelope may return DISARMED with its token.
        No exception is a successful receipt. Use get_fire_completion(token), not
        elapsed host time, DISARMED alone, or a predicted command sequence.
        """
        return self._request("FIRE", self._duration(duration_ms), allowed={LaserState.ARMED},
                             ready=True, startup=True, expected_generation=expected_stop_generation)

    def get_fire_completion(self, token: LaserFireToken | None = None) -> LaserFireCompletion | None:
        """Read a fresh, still-current natural FIRE receipt, without serial I/O.

        OFF/ESTOP, fault, disconnect/reset and new ARM/FOLLOW/FIRE invalidate it.
        Snapshot equality is provenance, not a cryptographic authorization token.
        """
        if token is not None and not isinstance(token, LaserFireToken):
            raise LaserError("a LaserFireToken is required")
        with self._lock:
            record, status = self._fire_completion, self.status
            if (record is None or not self.connected or status.state != LaserState.DISARMED
                    or status.comp_high is not False or status.fault is not None
                    or not status.startup_checked or status.completion_reason != "FIRE_DONE"
                    or status.stop_generation != record.completed_generation
                    or status.boot_nonce != record.boot_nonce or status.session_nonce != record.session_nonce
                    or token is not None and record.token != token):
                return None
            return record

    def _clear_fire(self) -> None:
        # State lock held by caller; no historic completion may authorize a job.
        self._fire_token = self._fire_completion = None

    def keepalive(self, *, expected_stop_generation=None) -> LaserStatus:
        return self._request("KEEP", allowed={LaserState.ARMED, LaserState.FOLLOW, LaserState.FIRING},
                             ready=True, startup=True, expected_generation=expected_stop_generation)

    def poll(self) -> LaserStatus:
        """STATUS query; NEVER renews the emission lease."""
        return self._request("STATUS")

    def check_ready(self, require_trigger: bool = True) -> LaserStatus:
        """Fresh connected supported state and required physical capabilities.

        Accepts DISARMED/ARMED/FOLLOW/FIRING. Caller checks its phase's allowed
        state. Does not require ARMED or renew a lease or confirm self-check.
        """
        if type(require_trigger) is not bool:
            raise LaserError("require_trigger must be bool")
        status = self.poll()
        with self._lock:
            self._check_ready(status, require_trigger)
        return status

    @staticmethod
    def _duration(value) -> int:
        if type(value) is not int or not 1 <= value <= MAX_WINDOW_MS:
            raise LaserError("exposure envelope must be integer milliseconds in 1..60000")
        return value

    def _check_ready(self, status: LaserStatus, trigger: bool) -> None:
        if status.stop_generation != self._generation:
            raise LaserCancelledError("readiness belongs to a cancelled generation")
        if (not self._connected or self._closing or status.firmware_version != FIRMWARE_VERSION
                or status.state.value not in _READY
                or time.monotonic() - status.last_seen_monotonic > self.STATUS_MAX_AGE_S):
            raise LaserError("laser status is not fresh, connected and ready")
        if not status.interlock_wired or not status.interlock_ok:
            raise LaserError("qualified independent inhibit is not wired/closed")
        if trigger and not status.trigger_wired:
            raise LaserError("motion trigger input is not qualified/wired")
        if status.watchdog_ms != WATCHDOG_MS or status.max_window_ms != MAX_WINDOW_MS:
            raise LaserError("unsupported watchdog/window capability")

    def _request(self, command, argument=None, *, urgent=False, handshake=False,
                 allowed=None, ready=False, trigger=False, startup=False, expected_generation=None):
        with self._lock:
            generation = self._generation
        if expected_generation is not None and (
                type(expected_generation) is not int or expected_generation != generation):
            raise LaserCancelledError("stop generation changed; old job must not resume")
        normal_acquired = False
        pending = None
        sequence = None
        try:
            if not urgent:
                normal_acquired = self._normal_lock.acquire(timeout=self.ACK_TIMEOUT_S)
                if not normal_acquired:
                    raise LaserTimeoutError("another normal laser command is still pending")
            if not self._write_lock.acquire(timeout=self.WRITE_TIMEOUT_S + 0.05):
                self._fault_link("WRITE_LOCK_TIMEOUT", send_stop=False)
                raise LaserTimeoutError("serial writer did not become available")
            try:
                with self._lock:
                    if self._serial is None:
                        raise LaserError("laser serial link is disconnected")
                    if not urgent and generation != self._generation:
                        raise LaserCancelledError("OFF cancelled the pending job")
                    if not urgent and not handshake and (not self._connected or self._closing):
                        raise LaserError("laser serial session is unavailable")
                    if urgent:
                        # Revoke completion immediately, but retain the issued
                        # token until the stop ACK to identify a FIRE_DONE frame
                        # already in transit. It cannot authorize the old job:
                        # the stop generation changes before releasing this lock.
                        self._fire_completion = None
                        self._generation += 1
                        self._status = replace(self._status, stop_generation=self._generation)
                        self._cancel_pending(LaserCancelledError("cancelled by OFF/ESTOP"))
                    if ready:
                        self._check_ready(self._status, trigger)
                    if allowed is not None and self._status.state not in allowed:
                        raise LaserError(f"{command} not allowed in {self._status.state.value}")
                    if startup and not self._status.startup_checked:
                        raise LaserError("human startup self-check confirmation is required")
                    if command == "FOLLOW" and self._status.trigger_active:
                        raise LaserError("motion trigger must be inactive before FOLLOW")
                    if handshake:
                        sequence, wire = 0, f"L2 HELLO {self._session}\n"
                    else:
                        if self._boot is None:
                            raise LaserError("no matching firmware boot nonce; OFF cannot be confirmed")
                        if self._sequence >= 0xFFFFFFFF:
                            raise LaserError("command sequence exhausted; reconnect required")
                        self._sequence += 1
                        sequence = self._sequence
                        wire = f"L2 {self._boot} {self._session} {sequence} {command}"
                        if argument is not None:
                            wire += f" {argument}"
                        wire = ("\n" if urgent else "") + wire + "\n"
                    pending = _Pending(command, threading.Event())
                    if command in {"ARM", "FOLLOW", "FIRE"}:
                        self._clear_fire()
                    if command == "FIRE":
                        pending.fire_token = LaserFireToken(sequence, generation, self._boot, self._session)
                        self._fire_token = pending.fire_token
                    self._pending[sequence] = pending
                    serial_port = self._serial
                encoded = wire.encode("ascii")
                try:
                    written = serial_port.write(encoded)
                    if written != len(encoded):
                        raise LaserError("short serial write")
                except Exception as exc:
                    self._fault_link(f"WRITE_FAILED: {exc}", send_stop=False)
                    raise LaserError(f"laser write failed: {exc}") from exc
            finally:
                self._write_lock.release()
            timeout = max(self.ACK_TIMEOUT_S, 0.75) if handshake else self.ACK_TIMEOUT_S
            if not pending.event.wait(timeout):
                self._fault_link(f"ACK_TIMEOUT:{command}")
                raise LaserTimeoutError(f"{command} acknowledgement timed out; output unconfirmed")
            with self._lock:
                if pending.error:
                    raise pending.error
                if not urgent and generation != self._generation:
                    completion = self.get_fire_completion(pending.fire_token) if pending.fire_token else None
                    if command == "FIRE" and completion is not None:
                        return self._status
                    raise LaserCancelledError("OFF/fault invalidated the command result")
                if pending.status is None:
                    raise LaserProtocolError("missing command status")
                return pending.status
        finally:
            if sequence is not None:
                with self._lock:
                    self._pending.pop(sequence, None)
            if normal_acquired:
                self._normal_lock.release()

    def _cancel_pending(self, error: BaseException, preserve_sequence=None) -> None:
        # Called with state lock; late ACKs from cancelled work must never make
        # an old ARMED/FIRING snapshot current again or block priority OFF.
        for sequence, pending in tuple(self._pending.items()):
            if sequence == preserve_sequence:
                continue
            self._cancelled.add(sequence)
            pending.error = error
            pending.event.set()
            self._pending.pop(sequence, None)
        if len(self._cancelled) > 64:
            self._cancelled = set(sorted(self._cancelled)[-64:])

    def _fault_link(self, reason: str, *, send_stop=True) -> None:
        with self._lock:
            self._link_fault = reason
            self._clear_fire()
            self._connected = False
            self._generation += 1
            self._status = replace(self._status, state=LaserState.UNKNOWN, comp_high=None,
                                   startup_checked=False, fault=reason, stop_generation=self._generation)
            self._cancel_pending(LaserError(reason))
            self._boot_seen.set()
        if send_stop:
            self._best_effort_stop()

    def _best_effort_stop(self) -> None:
        # No ACK wait, no retry loop, no lease renewal. Bare ESTOP is accepted
        # even with no session or after parser overflow/reset.
        if not self._write_lock.acquire(timeout=self.WRITE_TIMEOUT_S + 0.05):
            return
        try:
            with self._lock:
                serial_port = self._serial
            if serial_port is not None:
                try:
                    serial_port.write(b"\nESTOP\n")
                except Exception:
                    pass
        finally:
            self._write_lock.release()

    def _close_serial(self) -> None:
        with self._lock:
            serial_port = self._serial
            self._connected = False
            self._closing = True
            self._generation += 1
            self._clear_fire()
            self._reader_stop.set()
            self._cancel_pending(LaserCancelledError("serial session closed"))
            self._status = replace(self._status, state=LaserState.UNKNOWN, comp_high=None,
                                   startup_checked=False, stop_generation=self._generation)
        close_error = None
        if serial_port is not None:
            try:
                serial_port.close()
            except Exception as exc:
                close_error = exc
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(self.READ_TIMEOUT_S + self.WRITE_TIMEOUT_S + 0.2)
        reader_pending = reader is not None and reader.is_alive()
        with self._lock:
            # A failed close can leave the OS handle open. A lingering reader can
            # still be inside read(). Preserve both owners and refuse reconnect
            # until a subsequent cleanup confirms release; never reuse its event.
            if close_error is not None or reader_pending:
                reason = (f"CLOSE_UNCONFIRMED:{close_error}" if close_error is not None
                          else "READER_STOP_UNCONFIRMED")
                self._status = replace(self._status, state=LaserState.UNKNOWN, comp_high=None,
                                       startup_checked=False, fault=reason)
            else:
                self._serial = None
                self._reader = None
                self._closing = False
        if close_error is not None:
            raise LaserError(f"serial close failed: {close_error}") from close_error
        if reader_pending:
            raise LaserError("serial reader did not stop; retry disconnect before reconnect")

    def _read_loop(self, serial_port, stop_event) -> None:
        buffer = bytearray()
        carriage_return = False
        while not stop_event.is_set():
            try:
                chunk = serial_port.read(128)
                if stop_event.is_set():
                    return
                if not isinstance(chunk, bytes):
                    raise LaserProtocolError("serial reader returned non-bytes")
                for byte in chunk:
                    if byte == 10:
                        line = bytes(buffer)
                        buffer.clear()
                        carriage_return = False
                        if line:
                            self._handle_line(line.decode("ascii", errors="strict"))
                    elif byte == 13:
                        if carriage_return:
                            raise LaserProtocolError("repeated CR in serial frame")
                        carriage_return = True
                    elif carriage_return or byte < 32 or byte > 126 or len(buffer) >= self.MAX_FRAME_BYTES:
                        raise LaserProtocolError("invalid/oversized serial frame")
                    else:
                        buffer.append(byte)
                with self._lock:
                    stale_active = (self._connected and self._status.state.value in _ACTIVE
                                    and time.monotonic() - self._status.last_seen_monotonic > self.STATUS_MAX_AGE_S)
                if stale_active:
                    self._fault_link("STALE_ACTIVE_STATUS")
            except Exception as exc:
                with self._lock:
                    current = self._serial is serial_port and not stop_event.is_set()
                if current:
                    self._fault_link(f"READER_FAULT:{exc}")
                return

    def _handle_line(self, line: str) -> None:
        parts = line.split(" ")
        if len(parts) == 4 and parts[:2] == ["L2", "BOOT"]:
            if not re.fullmatch(r"[0-9a-f]{8}", parts[2]) or parts[3] != FIRMWARE_VERSION:
                raise LaserProtocolError("unknown firmware BOOT identity")
            with self._lock:
                if self._connected or self._pending:
                    raise LaserProtocolError("firmware reset during serial session")
                self._boot = parts[2]
                self._boot_seen.set()
            return
        if len(parts) != 22 or parts[0] != "L2" or parts[1] not in ("ACK", "EVENT"):
            raise LaserProtocolError("unknown or malformed firmware response")
        (_, kind, boot, session, seq, command, result, report, state, version,
         iw, tw, io, ta, comp, checked, wd, remaining, maximum, fault, fire_seq, completion_reason) = parts
        if (not re.fullmatch(r"[0-9a-f]{8}", boot) or boot == "00000000"
                or not re.fullmatch(r"[0-9a-f]{32}", session)
                or result not in ("OK", "ERR") or version != FIRMWARE_VERSION
                or not re.fullmatch(r"[A-Z_]{1,24}", fault)):
            raise LaserProtocolError("unsupported response identity/version")
        sequence, report_id = _uint(seq), _uint(report)
        if state not in {"BOOT_HOLD", "DISARMED", "ARMED", "FOLLOW", "FIRING", "FAULT"}:
            raise LaserProtocolError("unknown firmware state")
        status = LaserStatus(
            state=LaserState(state), firmware_version=version, interlock_wired=_bit(iw),
            trigger_wired=_bit(tw), interlock_ok=_bit(io), trigger_active=_bit(ta),
            comp_high=_bit(comp), last_seen_monotonic=time.monotonic(),
            fault=None if fault == "NONE" else fault, watchdog_ms=_uint(wd, 60000),
            startup_checked=_bit(checked), boot_nonce=boot, session_nonce=session,
            sequence=sequence, report_id=report_id, window_remaining_ms=_uint(remaining, MAX_WINDOW_MS),
            max_window_ms=_uint(maximum, MAX_WINDOW_MS),
            fire_sequence=_uint(fire_seq), completion_reason=completion_reason,
        )
        if (status.watchdog_ms != WATCHDOG_MS or status.max_window_ms != MAX_WINDOW_MS or report_id == 0
                or (status.interlock_ok and not status.interlock_wired)
                or (status.trigger_active and not status.trigger_wired)
                or (state in _ACTIVE and not (status.interlock_wired and status.interlock_ok and status.startup_checked))
                or (state in {"DISARMED", "ARMED", "FAULT"} and status.comp_high)
                or (state == "BOOT_HOLD" and (not status.comp_high or status.startup_checked))
                or (state == "FIRING" and not status.comp_high)
                or (state == "FOLLOW" and (not status.trigger_wired or status.comp_high != status.trigger_active))
                or (state not in {"FOLLOW", "FIRING"} and status.window_remaining_ms != 0)
                or (state in {"FOLLOW", "FIRING"} and status.window_remaining_ms == 0)
                or (state == "FAULT") != (status.fault is not None)
                or completion_reason not in {"NONE", "FIRE_DONE"}
                or (status.fire_sequence > 0) != (state == "FIRING" or completion_reason == "FIRE_DONE")
                or status.fire_sequence > sequence
                or (completion_reason == "FIRE_DONE" and (
                    state != "DISARMED" or result != "OK" or not status.startup_checked
                    or not status.interlock_wired or not status.interlock_ok))):
            raise LaserProtocolError("inconsistent or unsafe firmware status")
        with self._lock:
            if session != self._session or (self._boot is not None and boot != self._boot):
                raise LaserProtocolError("boot/session nonce mismatch")
            if kind == "ACK" and sequence in self._cancelled:
                return  # Never update state/freshness from an explicitly cancelled command.
            pending = self._pending.get(sequence)
            if (self._link_fault and not self._connected
                    and not (kind == "ACK" and pending and pending.command in {"OFF", "ESTOP"})):
                return  # A failed link cannot recover from unsolicited/late telemetry.
            if report_id <= self._last_report:
                raise LaserProtocolError("replayed/out-of-order firmware report")
            if kind == "ACK":
                if pending is None or pending.command != command:
                    raise LaserProtocolError("ACK command/sequence mismatch")
                if result == "OK":
                    expected_states = {
                        "HELLO": {"BOOT_HOLD", "DISARMED"}, "OFF": {"DISARMED"},
                        "ESTOP": {"FAULT"}, "CHECK": {"DISARMED"}, "ARM": {"ARMED"},
                        "FOLLOW": {"FOLLOW"}, "FIRE": {"FIRING", "DISARMED"}, "KEEP": _ACTIVE | {"DISARMED"},
                    }
                    if (command in expected_states and state not in expected_states[command]
                            or command == "CHECK" and not status.startup_checked
                            or command == "ESTOP" and status.fault != "ESTOP"
                            or command == "KEEP" and state == "DISARMED" and completion_reason != "FIRE_DONE"
                            or command == "FIRE" and (
                                status.fire_sequence != sequence
                                or state == "DISARMED" and completion_reason != "FIRE_DONE")):
                        raise LaserProtocolError("ACK does not confirm the requested operation")
            # EVENT reports firmware lastSeq, not the host's newest write. A
            # safetyTick can fault before the next request is read, so permit
            # that in-flight gap. Never regress behind an accepted ACK/EVENT or
            # advance beyond an issued request. Nonces/report_id remain strict;
            # explicitly cancelled ACKs above still cannot restore state.
            elif (command != "EVENT" or state not in {"DISARMED", "FAULT"}
                  or not self._last_received_sequence <= sequence <= self._sequence):
                raise LaserProtocolError("invalid unsolicited state transition")
            if self._status.firmware_version is not None and (
                status.interlock_wired != self._status.interlock_wired
                or status.trigger_wired != self._status.trigger_wired
            ):
                raise LaserProtocolError("firmware wiring capabilities changed within session")
            self._boot, self._last_report = boot, report_id
            self._last_received_sequence = max(self._last_received_sequence, sequence)
            natural_completion = completion_reason == "FIRE_DONE"
            if status.fire_sequence:
                token = self._fire_token
                if (token is None or token.fire_sequence != status.fire_sequence
                        or token.boot_nonce != boot or token.session_nonce != session):
                    raise LaserProtocolError("FIRE telemetry does not match an issued FIRE token")
                if (kind == "EVENT" and natural_completion
                        and token.started_generation != self._generation
                        and any(s > sequence and p.command in {"OFF", "ESTOP"}
                                for s, p in self._pending.items())):
                    # UART report ordering was checked above. Drain this known
                    # pre-stop completion without refreshing status, publishing
                    # a receipt, or cancelling the stop awaiting its own ACK.
                    return
                status = replace(status, fire_token=token)
                if natural_completion:
                    if self._fire_completion is None:
                        if self._generation != token.started_generation:
                            raise LaserProtocolError("FIRE completion belongs to a cancelled generation")
                        self._generation += 1
                        self._fire_completion = LaserFireCompletion(
                            token.fire_sequence, token.started_generation, boot, session, self._generation)
                        # Preserve FIRE's own accepted receipt, including an ACK
                        # followed by expiry before its waiting caller wakes.
                        self._cancel_pending(LaserCancelledError("natural FIRE completion"),
                                             preserve_sequence=token.fire_sequence)
                    elif self._generation != self._fire_completion.completed_generation:
                        raise LaserProtocolError("stale FIRE completion generation")
                elif self._generation != token.started_generation:
                    raise LaserProtocolError("FIRE telemetry resumed after completion/stop")
            elif state in {"DISARMED", "FAULT", "BOOT_HOLD"}:
                self._clear_fire()
            status = replace(status, stop_generation=self._generation)
            self._status = status
            if kind == "ACK":
                pending.status = status
                if result != "OK":
                    pending.error = LaserError(f"{command} rejected: {status.fault or state}")
                pending.event.set()
            elif not natural_completion:
                self._generation += 1
                self._status = replace(status, stop_generation=self._generation)
                self._cancel_pending(LaserCancelledError(f"firmware transition: {status.fault or state}"))
