"""Fake serial only. These tests never open a COM port, instantiate an SDK or upload."""

from dataclasses import FrozenInstanceError, replace
import math
import threading
import time

import pytest

from automatic_transfer_station.laser_controller import (
    FIRMWARE_VERSION, MAX_WINDOW_MS, WATCHDOG_MS, LaserController,
    LaserError, LaserCancelledError, LaserState, LaserStatus,
    LaserFireToken, LaserFireCompletion,
)


class FakeSerial:
    """Independent protocol peer with manually advanced firmware time.

    This is a host-driver test double, not evidence of electrical/firmware timing.
    ACK suppression/reordering and link exceptions are injected at byte level.
    """

    def __init__(self, *, interlock=True, trigger=True, **kwargs):
        self.options = kwargs
        self.condition = threading.Condition(threading.RLock())
        self.input = bytearray()
        self.tx_input = bytearray()
        self.closed = False
        self.disappeared = False
        self.boot = "00000001"
        self.session = ""
        self.seq = self.report_id = 0
        self.state = "BOOT_HOLD"
        self.comp = True
        self.checked = False
        self.iw, self.tw = interlock, trigger
        self.io = interlock
        self.trigger = False
        self.fault = "NONE"
        self.now = self.lease = self.window_at = self.window = 0
        self.fire_seq = 0
        self.completion_reason = "NONE"
        self.commands = []
        self.responses = []
        self.held = []
        self.hold = set()
        self.drop = set()
        self.hold_event = threading.Event()
        self.mutate = None
        self.write_error = None
        self.short_write = False
        self.inject(f"L2 BOOT {self.boot} {FIRMWARE_VERSION}\n")

    def inject(self, text):
        with self.condition:
            self.input.extend(text.encode("ascii") if isinstance(text, str) else text)
            self.condition.notify_all()

    def read(self, size):
        with self.condition:
            if not self.input and not self.closed and not self.disappeared:
                self.condition.wait(self.options.get("timeout", 0.01))
            if self.closed or self.disappeared:
                raise OSError("fake serial link removed")
            result = bytes(self.input[:size])
            del self.input[:size]
            return result

    def write(self, data):
        if self.write_error:
            raise self.write_error
        if self.short_write:
            return len(data) - 1
        with self.condition:
            if self.closed or self.disappeared:
                raise OSError("fake serial link removed")
            self.tx_input.extend(data)
            while b"\n" in self.tx_input:
                line, _, rest = self.tx_input.partition(b"\n")
                self.tx_input = bytearray(rest)
                if line:
                    self.command(line.decode("ascii"))
        return len(data)

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def dark(self, fault="NONE"):
        self.state = "DISARMED" if fault == "NONE" else "FAULT"
        self.comp = False
        self.fault = fault
        self.window = 0
        self.fire_seq = 0
        self.completion_reason = "NONE"
        if fault != "NONE":
            self.checked = False

    def report(self, command, sequence, ok=True, kind="ACK"):
        self.report_id += 1
        remaining = max(0, self.window - (self.now - self.window_at)) if self.state in ("FOLLOW", "FIRING") else 0
        fields = ["L2", kind, self.boot, self.session, str(sequence), command, "OK" if ok else "ERR",
                  str(self.report_id), self.state, FIRMWARE_VERSION,
                  *[str(int(v)) for v in (self.iw, self.tw, self.io, self.trigger, self.comp, self.checked)],
                  str(WATCHDOG_MS), str(remaining), str(MAX_WINDOW_MS), self.fault,
                  str(self.fire_seq), self.completion_reason]
        wire = " ".join(fields) + "\n"
        if self.mutate:
            wire = self.mutate(wire)
        self.responses.append(wire)
        if command in self.hold:
            self.held.append(wire)
            self.hold_event.set()
        elif command not in self.drop:
            self.inject(wire)

    def command(self, line):
        parts = line.split()
        if parts in (["OFF"], ["ESTOP"]):
            self.commands.append(parts[0])
            self.dark("ESTOP" if parts[0] == "ESTOP" else "NONE")
            if self.session:
                self.report("EVENT", self.seq, kind="EVENT", ok=self.fault == "NONE")
            return
        if len(parts) == 3 and parts[:2] == ["L2", "HELLO"]:
            if self.session:
                self.dark()
            self.session = parts[2]
            self.checked = False
            self.commands.append("HELLO")
            self.report("HELLO", 0)
            return
        if len(parts) not in (5, 6) or parts[0] != "L2":
            self.dark("PROTOCOL")
            return
        _, boot, session, sequence, command, *args = parts
        sequence = int(sequence)
        self.commands.append(command)
        if command in ("OFF", "ESTOP"):
            self.dark("ESTOP" if command == "ESTOP" else "NONE")
        if boot != self.boot or session != self.session or sequence <= self.seq:
            self.dark("REPLAY")
            self.report(command, sequence, False)
            return
        self.seq = sequence
        ok = True
        if command in ("OFF", "ESTOP", "STATUS"):
            pass
        elif command == "CHECK" and self.state == "DISARMED" and self.iw and self.io:
            self.checked = True
        elif command == "ARM" and self.state == "DISARMED" and self.checked and self.iw and self.io:
            self.state, self.lease = "ARMED", self.now
            self.fire_seq, self.completion_reason = 0, "NONE"
        elif command in ("FIRE", "FOLLOW") and self.state == "ARMED" and self.checked and self.io:
            duration = int(args[0])
            if not 1 <= duration <= MAX_WINDOW_MS or command == "FOLLOW" and not self.tw:
                self.dark("PROTOCOL")
                ok = False
            elif command == "FOLLOW" and self.trigger:
                self.dark("TRIGGER_ACTIVE")
                ok = False
            else:
                self.window, self.window_at = duration, self.now
                self.state = "FIRING" if command == "FIRE" else "FOLLOW"
                self.comp = command == "FIRE" or self.trigger
                self.fire_seq = sequence if command == "FIRE" else 0
                self.completion_reason = "NONE"
        elif (command == "KEEP" and self.state == "DISARMED" and self.completion_reason == "FIRE_DONE"
              and self.io and self.checked):
            pass  # A late KEEP reports the receipt, never renews a completed lease.
        elif command == "KEEP" and self.state in ("ARMED", "FOLLOW", "FIRING") and self.io:
            self.lease = self.now
        else:
            self.dark("STATE")
            ok = False
        self.report(command, sequence, ok)

    def advance(self, milliseconds):
        with self.condition:
            self.now += milliseconds
            if self.state not in ("ARMED", "FOLLOW", "FIRING"):
                return
            old = self.state
            if not self.io:
                self.dark("INTERLOCK")
            elif self.now - self.lease >= WATCHDOG_MS:
                self.dark("WATCHDOG")
            elif self.state in ("FOLLOW", "FIRING") and self.now - self.window_at >= self.window:
                completed = self.fire_seq if self.state == "FIRING" else 0
                self.dark()
                self.fire_seq = completed
                self.completion_reason = "FIRE_DONE" if completed else "NONE"
            elif self.state == "FOLLOW":
                self.comp = self.trigger
            if old != self.state:
                self.report("EVENT", self.seq, kind="EVENT", ok=self.fault == "NONE")


def eventually(predicate, timeout=0.5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("condition was not observed within bounded test deadline")
        time.sleep(0.002)


@pytest.fixture
def make_controller():
    created = []

    def make(**fake_options):
        peer = None

        def factory(**kwargs):
            nonlocal peer
            peer = FakeSerial(**kwargs, **fake_options)
            return peer

        controller = LaserController(factory)
        controller.CONNECT_TIMEOUT_S = 0.02
        controller.ACK_TIMEOUT_S = 0.08
        controller.READ_TIMEOUT_S = 0.01
        controller.WRITE_TIMEOUT_S = 0.03
        created.append(controller)
        controller.connect("FAKE_ONLY")
        return controller, peer

    yield make
    for controller in created:
        try:
            controller.disconnect()
        except LaserError:
            pass
        assert controller._reader is None or not controller._reader.is_alive()


def prepare(controller):
    controller.off()
    controller.confirm_startup_check()
    return controller.status.stop_generation


def start_fire(controller, duration=20):
    generation = prepare(controller)
    controller.arm(expected_stop_generation=generation)
    return controller.fire(duration, expected_stop_generation=generation)


def test_fire_receipt_uses_actual_sequence_and_natural_expiry_only(make_controller):
    controller, peer = make_controller()
    generation = prepare(controller)
    controller.arm(expected_stop_generation=generation)
    before = controller.keepalive(expected_stop_generation=generation)
    controller.poll()  # A monitor wins the next sequence; do not predict it.
    accepted = controller.fire(20, expected_stop_generation=generation)
    token = accepted.fire_token
    assert isinstance(token, LaserFireToken)
    assert token.fire_sequence == accepted.sequence == before.sequence + 2
    assert token.started_generation == generation
    assert controller.get_fire_completion(token) is None
    peer.advance(20)
    eventually(lambda: controller.get_fire_completion(token) is not None)
    record = controller.get_fire_completion(token)
    assert isinstance(record, LaserFireCompletion) and record.token == token
    assert record.completed_generation == generation + 1
    assert controller.status.completion_reason == "FIRE_DONE"
    assert controller.status.comp_high is False
    assert controller.poll().stop_generation == generation + 1  # No repeated increment.
    assert controller.get_fire_completion(token) == record
    with pytest.raises(FrozenInstanceError):
        record.fire_sequence = 999
    with pytest.raises(FrozenInstanceError):
        token.fire_sequence = 999
    assert controller.get_fire_completion(replace(token, fire_sequence=token.fire_sequence - 1)) is None
    with pytest.raises(LaserError):
        controller.get_fire_completion(123)


def test_short_fire_expired_in_its_own_ack_returns_bound_token(make_controller):
    controller, peer = make_controller()
    report = peer.report

    def expire_before_ack(command, sequence, *args, **kwargs):
        if command == "FIRE":
            peer.now += peer.window
            peer.dark()
            peer.fire_seq, peer.completion_reason = sequence, "FIRE_DONE"
        report(command, sequence, *args, **kwargs)

    peer.report = expire_before_ack
    accepted = start_fire(controller, 1)
    assert accepted.state == LaserState.DISARMED
    assert accepted.fire_token.fire_sequence == accepted.sequence
    record = controller.get_fire_completion(accepted.fire_token)
    assert record.completed_generation == accepted.stop_generation


def test_short_fire_event_after_ack_before_call_returns_is_not_a_cancelled_receipt(make_controller):
    controller, peer = make_controller()
    handle = controller._handle_line
    peer.drop.add("EVENT")

    def expire_before_waking_caller(line):
        with controller._lock:
            handle(line)
            if line.split()[5:7] == ["FIRE", "OK"]:
                peer.advance(1)
                handle(peer.responses[-1].strip())

    controller._handle_line = expire_before_waking_caller
    accepted = start_fire(controller, 1)
    assert accepted.state == LaserState.DISARMED
    assert controller.get_fire_completion(accepted.fire_token) is not None


@pytest.mark.parametrize("stop_method", ["off", "emergency_off"])
def test_inflight_fire_done_during_priority_stop_does_not_fault_or_authorize(make_controller, stop_method):
    controller, peer = make_controller()
    token = start_fire(controller, 1).fire_token
    command = peer.command
    seen = []

    def finish_before_stop_is_processed(line):
        if (peer.state == "FIRING"
                and line.split()[4:5] == ["OFF" if stop_method == "off" else "ESTOP"]):
            # The host has invalidated the job and written its stop, but the
            # MCU's earlier natural-completion frame is still in transit.
            peer.drop.add("EVENT")
            peer.advance(1)
            before = controller.status
            controller._handle_line(peer.responses[-1].strip())
            assert controller.status == before  # No freshness or completion authority.
            seen.append(controller.get_fire_completion(token))
        command(line)

    peer.command = finish_before_stop_is_processed
    status = getattr(controller, stop_method)()
    assert seen == [None]
    assert controller.connected and status.comp_high is False
    assert status.state == (LaserState.DISARMED if stop_method == "off" else LaserState.FAULT)
    assert status.fault == (None if stop_method == "off" else "ESTOP")
    assert controller.get_fire_completion(token) is None
    assert controller.status.completion_reason == "NONE"
    assert peer.comp is False


@pytest.mark.parametrize("ending", ["off", "emergency_off", "bare_off", "interlock", "watchdog", "reset", "disconnect"])
def test_interrupted_fire_never_creates_completion(make_controller, ending):
    controller, peer = make_controller()
    token = start_fire(controller, 600).fire_token
    if ending == "bare_off":
        peer.command("OFF")
        eventually(lambda: controller.status.state == LaserState.DISARMED)
    elif ending == "interlock":
        peer.io = False
        peer.advance(1)
        eventually(lambda: controller.status.state == LaserState.FAULT)
    elif ending == "watchdog":
        peer.advance(WATCHDOG_MS)
        eventually(lambda: controller.status.state == LaserState.FAULT)
    elif ending == "reset":
        peer.inject(f"L2 BOOT 00000002 {FIRMWARE_VERSION}\n")
        eventually(lambda: not controller.connected)
    else:
        getattr(controller, ending)()
    assert controller.get_fire_completion(token) is None


@pytest.mark.parametrize("invalidate", ["off", "emergency_off", "arm", "stale", "disconnect"])
def test_completed_fire_receipt_invalidated_before_it_can_authorize_later_work(make_controller, invalidate):
    controller, peer = make_controller()
    token = start_fire(controller, 1).fire_token
    peer.advance(1)
    eventually(lambda: controller.get_fire_completion(token) is not None)
    if invalidate == "stale":
        with controller._lock:
            controller._status = replace(controller._status, last_seen_monotonic=time.monotonic() - 1)
    else:
        getattr(controller, invalidate)()
    assert controller.get_fire_completion(token) is None


def test_follow_window_expiry_is_not_fire_completion(make_controller):
    controller, peer = make_controller()
    generation = prepare(controller)
    controller.arm(expected_stop_generation=generation)
    controller.follow(1, expected_stop_generation=generation)
    peer.advance(1)
    eventually(lambda: controller.status.state == LaserState.DISARMED)
    assert controller.status.stop_generation == generation + 1
    assert controller.status.completion_reason == "NONE"
    assert controller.get_fire_completion() is None


@pytest.mark.parametrize("field,value", [(20, "0"), (20, "999"), (21, "OFF"), (8, "ARMED"), (15, "0")])
def test_unbound_or_inconsistent_fire_completion_fails_link(make_controller, field, value):
    controller, peer = make_controller()
    token = start_fire(controller, 1).fire_token

    def corrupt(wire):
        parts = wire.split()
        parts[field] = value
        return " ".join(parts) + "\n"

    peer.mutate = corrupt
    peer.advance(1)
    eventually(lambda: not controller.connected)
    assert controller.get_fire_completion(token) is None


def test_legacy_v20_status_is_rejected_not_guessed_as_completion(make_controller):
    controller, peer = make_controller()
    peer.mutate = lambda wire: " ".join(wire.split()[:20]).replace(FIRMWARE_VERSION, "ATS-LASER-2.0") + "\n"
    with pytest.raises(LaserError):
        controller.poll()
    assert not controller.connected


@pytest.mark.parametrize("drop_event", [False, True])
def test_natural_expiry_racing_inflight_keep_preserves_receipt_without_renewal(make_controller, drop_event):
    controller, peer = make_controller()
    accepted = start_fire(controller, 20)
    token, lease = accepted.fire_token, peer.lease
    command = peer.command
    if drop_event:
        peer.drop.add("EVENT")

    def expire_before_keep(line):
        if line.endswith(" KEEP"):
            peer.advance(20)
        command(line)

    peer.command = expire_before_keep
    with pytest.raises(LaserCancelledError):
        controller.keepalive(expected_stop_generation=token.started_generation)
    assert controller.get_fire_completion(token) is not None
    assert peer.lease == lease and not peer.comp and peer.state == "DISARMED"
    assert controller.off().startup_checked  # No spurious STATE fault from the late KEEP.
    assert controller.get_fire_completion(token) is None


def test_watchdog_wins_over_same_tick_fire_window_expiry(make_controller):
    controller, peer = make_controller()
    accepted = start_fire(controller, WATCHDOG_MS)
    peer.advance(WATCHDOG_MS)
    eventually(lambda: controller.status.state == LaserState.FAULT)
    assert controller.status.fault == "WATCHDOG"
    assert controller.get_fire_completion(accepted.fire_token) is None


def test_default_status_and_immutable_fields():
    controller = LaserController(lambda **_: pytest.fail("must not open a serial port"))
    assert not controller.connected
    assert controller.status == LaserStatus()
    with pytest.raises(FrozenInstanceError):
        controller.status.state = LaserState.ARMED
    with pytest.raises(LaserError):
        controller.poll()


def test_handshake_preserves_manufacturer_boot_high_and_uses_bounded_serial_io(make_controller):
    controller, peer = make_controller()
    status = controller.status
    assert status.state == LaserState.BOOT_HOLD and status.comp_high
    assert controller.connected
    assert status.firmware_version == FIRMWARE_VERSION
    assert status.watchdog_ms == WATCHDOG_MS
    assert status.boot_nonce == peer.boot and status.session_nonce == peer.session
    assert peer.commands == ["HELLO"]
    assert peer.options == dict(port="FAKE_ONLY", baudrate=115200, timeout=0.01, write_timeout=0.03)
    with pytest.raises(LaserError):
        controller.check_ready()
    assert peer.comp  # Merely querying readiness does not force a LOW boot.


def test_unwired_shipped_capabilities_cannot_arm_follow_or_fire(make_controller):
    controller, peer = make_controller(interlock=False, trigger=False)
    controller.off()
    for operation in (controller.arm, controller.confirm_startup_check,
                      lambda: controller.follow(100), lambda: controller.fire(100),
                      lambda: controller.check_ready(False)):
        with pytest.raises(LaserError):
            operation()
    assert not controller.status.interlock_wired
    assert not controller.status.trigger_wired
    assert not any(command in ("ARM", "FOLLOW", "FIRE", "CHECK") for command in peer.commands)
    assert not peer.comp


def test_human_confirmation_and_explicit_arm_are_required_after_stop(make_controller):
    controller, peer = make_controller()
    controller.off()
    assert controller.check_ready().state == LaserState.DISARMED
    with pytest.raises(LaserError, match="self-check"):
        controller.arm()
    generation = prepare(controller)
    assert controller.arm(expected_stop_generation=generation).state == LaserState.ARMED
    controller.off()
    assert controller.status.startup_checked  # Normal OFF does not redo manufacturer self-check.
    with pytest.raises(LaserError):
        controller.fire(20)
    controller.arm()
    controller.emergency_off()
    assert controller.status.state == LaserState.FAULT
    assert controller.status.fault == "ESTOP"
    assert not controller.status.startup_checked and not peer.comp
    controller.off()
    with pytest.raises(LaserError, match="self-check"):
        controller.arm()


def test_trigger_required_only_for_follow_and_check_ready_argument(make_controller):
    controller, _ = make_controller(trigger=False)
    prepare(controller)
    assert controller.check_ready(False).state == LaserState.DISARMED
    with pytest.raises(LaserError, match="trigger"):
        controller.check_ready(True)
    controller.arm()
    with pytest.raises(LaserError, match="trigger"):
        controller.follow(100)
    assert controller.fire(100).state == LaserState.FIRING


@pytest.mark.parametrize("state", ["DISARMED", "ARMED", "FOLLOW", "FIRING"])
def test_check_ready_supports_each_job_phase_without_renewal(make_controller, state):
    controller, peer = make_controller()
    prepare(controller)
    if state != "DISARMED":
        controller.arm()
    if state == "FOLLOW":
        controller.follow(1000)
    elif state == "FIRING":
        controller.fire(1000)
    lease = peer.lease
    peer.advance(100)
    assert controller.check_ready().state.value == state
    assert peer.lease == lease
    assert peer.commands[-1] == "STATUS"


@pytest.mark.parametrize("method", ["fire", "follow"])
@pytest.mark.parametrize("value", [0, -1, 60001, True, 1.0, "100", None, math.nan, math.inf, 10**100])
def test_invalid_exposure_envelope_never_reaches_wire(make_controller, method, value):
    controller, peer = make_controller()
    generation = prepare(controller)
    controller.arm(expected_stop_generation=generation)
    before = list(peer.commands)
    with pytest.raises(LaserError):
        getattr(controller, method)(value)
    assert peer.commands == before


@pytest.mark.parametrize("duration", [1, 60000])
def test_valid_fire_envelope_bounds_and_expiry_latches_disarmed(make_controller, duration):
    controller, peer = make_controller()
    prepare(controller)
    controller.arm()
    status = controller.fire(duration)
    assert status.comp_high and status.window_remaining_ms == duration
    # Keep renewing manually while advancing fake time, never extend the window.
    while duration > 400:
        peer.advance(400)
        controller.keepalive()
        duration -= 400
    peer.advance(duration)
    eventually(lambda: controller.status.state == LaserState.DISARMED)
    assert not peer.comp
    with pytest.raises(LaserError):
        controller.keepalive()
    with pytest.raises(LaserError):
        controller.fire(10)


def test_follow_comp_tracks_motion_ttl_only_within_armed_lease(make_controller):
    controller, peer = make_controller()
    prepare(controller)
    controller.arm()
    assert not controller.follow(1000).comp_high
    peer.trigger = True
    peer.advance(10)
    assert controller.poll().comp_high
    peer.trigger = False
    peer.advance(10)
    assert not controller.poll().comp_high
    controller.off()
    peer.trigger = True
    peer.advance(10)
    assert not controller.poll().comp_high
    assert peer.state == "DISARMED"


@pytest.mark.parametrize("mode", ["ARMED", "FOLLOW", "FIRING"])
def test_watchdog_covers_every_armed_state_and_status_does_not_renew(make_controller, mode):
    controller, peer = make_controller()
    prepare(controller)
    controller.arm()
    if mode == "FOLLOW":
        controller.follow(1000)
    elif mode == "FIRING":
        controller.fire(1000)
    peer.advance(200)
    controller.poll()
    peer.advance(200)
    controller.check_ready()
    peer.advance(100)
    eventually(lambda: controller.status.state == LaserState.FAULT)
    assert controller.status.fault == "WATCHDOG"
    assert not controller.status.comp_high
    assert "KEEP" not in peer.commands
    with pytest.raises(LaserError):
        controller.arm()


def test_only_explicit_keepalive_renews_and_never_auto_resumes(make_controller):
    controller, peer = make_controller()
    prepare(controller)
    controller.arm()
    peer.advance(400)
    controller.keepalive()
    assert peer.lease == 400
    peer.advance(499)
    assert controller.poll().state == LaserState.ARMED
    peer.advance(1)
    eventually(lambda: controller.status.fault == "WATCHDOG")
    assert peer.commands.count("KEEP") == 1
    controller.off()
    peer.advance(1000)
    assert controller.poll().state == LaserState.DISARMED


def test_reader_never_generates_automatic_keepalive(make_controller):
    controller, peer = make_controller()
    prepare(controller)
    controller.arm()
    time.sleep(0.10)
    assert "KEEP" not in peer.commands


def test_interlock_open_during_exposure_fails_dark_and_invalidates_jobs(make_controller):
    controller, peer = make_controller()
    generation = prepare(controller)
    controller.arm()
    controller.fire(1000)
    peer.io = False
    peer.advance(1)
    eventually(lambda: controller.status.fault == "INTERLOCK")
    assert not controller.status.comp_high
    assert controller.status.stop_generation > generation
    with pytest.raises(LaserError):
        controller.check_ready(False)


@pytest.mark.parametrize("urgent", ["off", "emergency_off"])
def test_priority_off_preempts_pending_ack_and_late_armed_ack_cannot_restore_state(make_controller, urgent):
    controller, peer = make_controller()
    generation = prepare(controller)
    peer.hold.add("ARM")
    outcome = []

    def arm():
        try:
            controller.arm(expected_stop_generation=generation)
        except BaseException as error:
            outcome.append(error)

    worker = threading.Thread(target=arm)
    worker.start()
    assert peer.hold_event.wait(0.3)
    started = time.monotonic()
    stopped = getattr(controller, urgent)()
    assert time.monotonic() - started < controller.ACK_TIMEOUT_S
    worker.join(0.3)
    assert not worker.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], LaserCancelledError)
    assert not stopped.comp_high
    peer.inject(peer.held[0])
    time.sleep(0.02)
    assert controller.status.state == stopped.state
    assert controller.status.last_seen_monotonic == stopped.last_seen_monotonic
    assert controller.connected
    with pytest.raises(LaserCancelledError):
        controller.arm(expected_stop_generation=generation)


def test_queued_normal_request_cannot_write_after_priority_off(make_controller):
    controller, peer = make_controller()
    prepare(controller)
    peer.hold.add("STATUS")
    outcomes = []

    def operation(call):
        try:
            call()
        except LaserError as error:
            outcomes.append(error)

    first = threading.Thread(target=operation, args=(controller.poll,))
    first.start()
    assert peer.hold_event.wait(0.3)
    second = threading.Thread(target=operation, args=(controller.arm,))
    second.start()
    time.sleep(0.01)
    controller.off()
    first.join(0.3)
    second.join(0.3)
    assert not first.is_alive() and not second.is_alive()
    assert len(outcomes) == 2
    assert "ARM" not in peer.commands


@pytest.mark.parametrize("index,value", [
    (2, "00000002"), (3, "0" * 32), (4, "999"), (5, "ARM"),
    (8, "MAGIC"), (9, "legacy-1.0"), (10, "true"), (16, "0"),
    (16, "1000"), (18, "999999"), (17, "nan"), (7, "0"),
])
def test_mismatched_or_malformed_ack_fails_closed(make_controller, index, value):
    controller, peer = make_controller()
    prepare(controller)

    def mutate(wire):
        parts = wire.strip().split(" ")
        parts[index] = value
        return " ".join(parts) + "\n"

    peer.mutate = mutate
    with pytest.raises(LaserError):
        controller.poll()
    assert not controller.connected
    assert controller.status.state == LaserState.UNKNOWN
    assert controller.status.comp_high is None
    assert "ESTOP" in peer.commands


def test_duplicate_ack_is_not_accepted_as_new_status(make_controller):
    controller, peer = make_controller()
    controller.off()
    reply = peer.responses[-1]
    peer.inject(reply)
    eventually(lambda: not controller.connected)
    assert controller.status.state == LaserState.UNKNOWN


def test_ack_must_confirm_off_not_merely_echo_its_command(make_controller):
    controller, peer = make_controller()
    prepare(controller)

    def pretend_still_firing(wire):
        parts = wire.strip().split(" ")
        if parts[5] == "OFF":
            parts[8], parts[14], parts[17] = "FIRING", "1", "20"
        return " ".join(parts) + "\n"

    peer.mutate = pretend_still_firing
    with pytest.raises(LaserError):
        controller.off()
    assert controller.status.comp_high is None


@pytest.mark.parametrize("wire", [b"OK\n", b"L2 ACK broken\n", b"x" * 300, b"\xff\n", b"bad\x00\n"])
def test_old_firmware_garbage_binary_and_oversized_frames_reject(make_controller, wire):
    controller, peer = make_controller()
    peer.inject(wire)
    eventually(lambda: not controller.connected)
    assert controller.status.state == LaserState.UNKNOWN


def test_reset_cannot_inherit_session_confirmation_or_emit_automatically(make_controller):
    controller, peer = make_controller()
    prepare(controller)
    controller.arm()
    peer.inject(f"L2 BOOT 00000002 {FIRMWARE_VERSION}\n")
    eventually(lambda: not controller.connected)
    assert not controller.status.startup_checked
    assert controller.status.comp_high is None
    with pytest.raises(LaserError):
        controller.arm()
    assert peer.commands.count("HELLO") == 1


def test_reader_disconnect_fails_waiting_request_promptly(make_controller):
    controller, peer = make_controller()
    peer.hold.add("STATUS")
    result = []

    def poll():
        try:
            controller.poll()
        except LaserError as error:
            result.append(error)

    worker = threading.Thread(target=poll)
    worker.start()
    assert peer.hold_event.wait(0.3)
    with peer.condition:
        peer.disappeared = True
        peer.condition.notify_all()
    worker.join(0.3)
    assert not worker.is_alive() and result
    assert not controller.connected and controller.status.comp_high is None


def test_stale_disarmed_status_can_refresh_by_poll_without_lease(make_controller):
    controller, peer = make_controller()
    controller.off()
    with controller._lock:
        controller._status = replace(controller._status, last_seen_monotonic=time.monotonic() - 2)
    assert controller.status.state == LaserState.UNKNOWN
    assert controller.check_ready().state == LaserState.DISARMED
    assert "KEEP" not in peer.commands


def test_stale_active_status_fails_closed_without_hidden_keepalive(make_controller):
    controller, peer = make_controller()
    prepare(controller)
    controller.arm()
    controller.STATUS_MAX_AGE_S = 0.03
    eventually(lambda: not controller.connected)
    assert "KEEP" not in peer.commands
    assert "ESTOP" in peer.commands


def test_ack_timeout_is_bounded_and_marks_output_unconfirmed(make_controller):
    controller, peer = make_controller()
    peer.drop.add("STATUS")
    started = time.monotonic()
    with pytest.raises(LaserError, match="timed out"):
        controller.poll()
    assert time.monotonic() - started < 0.4
    assert not controller.connected
    assert controller.status.comp_high is None
    assert "ESTOP" in peer.commands


@pytest.mark.parametrize("failure", ["timeout", "short"])
def test_write_timeout_or_short_write_cannot_be_treated_as_ack(make_controller, failure):
    controller, peer = make_controller()
    if failure == "timeout":
        peer.write_error = TimeoutError("fake bounded write timeout")
    else:
        peer.short_write = True
    with pytest.raises(LaserError):
        controller.off()
    assert not controller.connected and controller.status.comp_high is None


def test_disconnect_requires_off_ack_and_does_not_claim_low_after_dtr_close(make_controller):
    controller, peer = make_controller()
    prepare(controller)
    controller.arm()
    result = controller.disconnect()
    assert peer.commands[-1] == "OFF"
    assert peer.closed and not controller.connected
    assert result.state == LaserState.DISCONNECTED
    assert result.comp_high is None and result.fault is None


def test_disconnect_reports_unconfirmed_off_but_still_closes(make_controller):
    controller, peer = make_controller()
    peer.drop.add("OFF")
    with pytest.raises(LaserError, match="OFF unconfirmed"):
        controller.disconnect()
    assert peer.closed and not controller.connected
    assert controller.status.fault == "OFF_UNCONFIRMED"
    assert controller.status.comp_high is None


def test_failed_serial_close_retains_resource_until_retry_before_reconnect(make_controller, monkeypatch):
    controller, peer = make_controller()
    prepare(controller)
    reader, close = controller._reader, peer.close
    attempts = []

    def fail_close():
        attempts.append(True)
        raise OSError("injected close failure")

    monkeypatch.setattr(peer, "close", fail_close)
    try:
        with pytest.raises(LaserError, match="close failed"):
            controller.disconnect()
        assert controller._serial is peer and not peer.closed
        assert controller.has_serial_resource and not controller.connected
        assert controller.status.state == LaserState.UNKNOWN
        assert controller.status.comp_high is None and not controller.status.startup_checked
        assert "CLOSE_UNCONFIRMED" in controller.status.fault
        assert not reader.is_alive()
        with pytest.raises(LaserError, match="disconnect the existing"):
            controller.connect("FAKE_SECOND")
        with pytest.raises(LaserError, match="close failed"):
            controller.disconnect()
        assert len(attempts) == 2
    finally:
        monkeypatch.setattr(peer, "close", close)
        try:
            controller.disconnect()
        except LaserError:  # Reader stopped; an OFF ACK is no longer available.
            pass
        close()
    assert peer.closed and not controller.has_serial_resource
    controller.connect("FAKE_SECOND")
    assert controller.connected and controller._serial is not peer
    assert not reader.is_alive()


def test_lingering_reader_blocks_reconnect_and_drops_its_late_frame(make_controller, monkeypatch):
    controller, peer = make_controller()
    controller.off()
    entered, release = threading.Event(), threading.Event()
    reader = controller._reader

    def stalled_read(size):
        entered.set()
        assert release.wait(3)
        return b"L2 BOOT 12345678 ATS-LASER-2.1\n"

    monkeypatch.setattr(peer, "read", stalled_read)
    assert entered.wait(1)
    try:
        with pytest.raises(LaserError, match="reader.*stop"):
            controller.disconnect()
        assert peer.closed and controller.has_serial_resource
        assert controller.status.state == LaserState.UNKNOWN
        assert controller.status.comp_high is None
        with pytest.raises(LaserError, match="disconnect the existing"):
            controller.connect("FAKE_SECOND")
        old_boot = controller.status.boot_nonce
    finally:
        release.set()
        reader.join(1)
    assert not reader.is_alive()
    assert controller._boot == old_boot  # A stopped reader cannot mutate its session.
    with pytest.raises(LaserError):
        controller.disconnect()
    assert not controller.has_serial_resource
    controller.connect("FAKE_SECOND")
    assert controller.connected


def test_failed_connect_cleanup_retains_port_when_close_fails(monkeypatch):
    peer = FakeSerial()
    peer.input.clear()
    peer.inject("OLD-LASER READY\n")
    close = peer.close
    controller = LaserController(lambda **_: peer)
    controller.CONNECT_TIMEOUT_S = 0.01
    controller.ACK_TIMEOUT_S = 0.02

    def fail_close():
        raise OSError("injected close failure")

    monkeypatch.setattr(peer, "close", fail_close)
    try:
        with pytest.raises(LaserError, match="close failed"):
            controller.connect("FAKE_ONLY")
        assert controller.has_serial_resource and controller._serial is peer
        assert not controller.connected and controller.status.comp_high is None
        with pytest.raises(LaserError, match="disconnect the existing"):
            controller.connect("FAKE_SECOND")
    finally:
        monkeypatch.setattr(peer, "close", close)
        try:
            controller.disconnect()
        except LaserError:
            pass
        close()
    assert not controller.has_serial_resource


def test_old_firmware_connection_is_rejected_and_closed_without_enable():
    peers = []

    def factory(**kwargs):
        peer = FakeSerial(**kwargs)
        peer.input.clear()
        peer.inject("OLD-LASER READY\n")
        peers.append(peer)
        return peer

    controller = LaserController(factory)
    controller.CONNECT_TIMEOUT_S = 0.01
    with pytest.raises(LaserError):
        controller.connect("FAKE_ONLY")
    assert peers[0].closed and not controller.connected
    assert "ARM" not in peers[0].commands


def test_reconnect_has_new_session_and_rejects_response_from_previous_connection(make_controller):
    controller, peer = make_controller()
    controller.off()
    old = peer.responses[-1]
    old_session = controller.status.session_nonce
    controller.disconnect()
    controller.connect("FAKE_ONLY")
    new_peer = controller._serial
    assert controller.status.session_nonce != old_session
    new_peer.inject(old)
    eventually(lambda: not controller.connected)


@pytest.mark.parametrize("value", [None, "", " ", 1, True])
def test_invalid_port_does_not_call_factory(value):
    controller = LaserController(lambda **_: pytest.fail("unexpected port open"))
    with pytest.raises(LaserError):
        controller.connect(value)


def test_generation_must_be_real_integer_and_match_current_job(make_controller):
    controller, _ = make_controller()
    prepare(controller)
    for generation in (True, "1", -1, controller.status.stop_generation - 1):
        with pytest.raises(LaserCancelledError):
            controller.arm(expected_stop_generation=generation)


def test_readiness_cannot_return_a_status_invalidated_by_concurrent_off(make_controller, monkeypatch):
    controller, _ = make_controller()
    prepare(controller)
    original_poll = controller.poll

    def poll_then_stop():
        result = original_poll()
        controller.off()
        return result

    monkeypatch.setattr(controller, "poll", poll_then_stop)
    with pytest.raises(LaserCancelledError, match="generation"):
        controller.check_ready()


def test_wiring_capabilities_cannot_change_inside_one_session(make_controller):
    controller, peer = make_controller()
    controller.off()
    peer.tw = False
    with pytest.raises(LaserError):
        controller.poll()
    assert not controller.connected


def test_unknown_inconsistent_active_state_is_not_a_ready_status(make_controller):
    controller, peer = make_controller()
    prepare(controller)

    def invalid_active(wire):
        parts = wire.strip().split()
        parts[8], parts[12] = "ARMED", "0"  # active with open inhibit
        return " ".join(parts) + "\n"

    peer.mutate = invalid_active
    with pytest.raises(LaserError):
        controller.poll()
    assert controller.status.state == LaserState.UNKNOWN


def test_current_command_rejected_by_firmware_exposes_fault_without_success(make_controller):
    controller, peer = make_controller()
    prepare(controller)
    peer.io = False  # Opens after cached readiness, before ARM reaches firmware.
    with pytest.raises(LaserError):
        controller.arm()
    assert controller.status.state == LaserState.FAULT
    assert not controller.status.comp_high


def test_replayed_report_id_with_otherwise_matching_ack_cannot_refresh_status(make_controller):
    controller, peer = make_controller()
    controller.off()
    previous = controller.status.report_id

    def replay_number(wire):
        parts = wire.strip().split()
        parts[7] = str(previous)
        return " ".join(parts) + "\n"

    peer.mutate = replay_number
    with pytest.raises(LaserError):
        controller.poll()
    assert not controller.connected


def test_failed_link_cannot_recover_from_late_unsolicited_telemetry(make_controller):
    controller, peer = make_controller()
    peer.drop.add("STATUS")
    with pytest.raises(LaserError):
        controller.poll()
    time.sleep(0.03)  # Allow the bare ESTOP's unsolicited report to arrive.
    assert not controller.connected
    assert controller.status.state == LaserState.UNKNOWN
    assert controller.status.comp_high is None


def test_no_extra_arguments_or_missing_fields_are_accepted_as_firmware_status(make_controller):
    controller, peer = make_controller()
    peer.mutate = lambda wire: wire.rstrip() + " extra\n"
    with pytest.raises(LaserError):
        controller.poll()
    assert not controller.connected


def test_crlf_response_is_accepted_without_changing_fields(make_controller):
    controller, peer = make_controller()
    peer.mutate = lambda wire: wire.replace("\n", "\r\n")
    assert controller.off().state == LaserState.DISARMED


@pytest.mark.parametrize("change", [lambda wire: wire.replace("DISARMED", "DIS\rARMED"),
                                     lambda wire: wire.replace("\n", "\r\r\n")])
def test_embedded_or_repeated_cr_is_not_silently_removed(make_controller, change):
    controller, peer = make_controller()
    peer.mutate = change
    with pytest.raises(LaserError):
        controller.off()
    assert not controller.connected


@pytest.mark.parametrize("operation", ["poll", "keepalive"])
def test_fault_event_before_pending_command_is_read_preserves_interlock(
        make_controller, monkeypatch, operation):
    controller, peer = make_controller()
    generation = prepare(controller)
    controller.arm(expected_stop_generation=generation)
    controller.follow(1000, expected_stop_generation=generation)
    previous = controller.status
    original_command = peer.command
    deferred, errors = [], []
    received = threading.Event()
    verb = "STATUS" if operation == "poll" else "KEEP"

    def defer_command(line):
        if len(line.split()) == 5 and line.split()[4] == verb:
            deferred.append(line)  # Host write succeeds; firmware has NOT read it.
            received.set()
        else:
            original_command(line)

    monkeypatch.setattr(peer, "command", defer_command)

    def request():
        try:
            getattr(controller, operation)()
        except LaserError as error:
            errors.append(error)

    worker = threading.Thread(target=request)
    worker.start()
    try:
        assert received.wait(0.3)
        assert int(deferred[0].split()[3]) == previous.sequence + 1
        assert peer.seq == previous.sequence
        peer.io = False
        peer.advance(1)  # safetyTick's EVENT carries lastSeq=N-1, not host's N.
        worker.join(0.3)
        assert not worker.is_alive()
        assert controller.connected
        stopped = controller.status
        assert stopped.state is LaserState.FAULT and stopped.fault == "INTERLOCK"
        assert stopped.comp_high is False and not stopped.startup_checked
        assert stopped.sequence == previous.sequence
        assert stopped.report_id > previous.report_id
        assert stopped.stop_generation > generation
        assert len(errors) == 1 and isinstance(errors[0], LaserCancelledError)
        assert "INTERLOCK" in str(errors[0])
        assert "ESTOP" not in peer.commands  # No spurious protocol-failure recovery.
        # The now-cancelled request can subsequently be read and acknowledged;
        # its late ACK must not replace the original fault or refresh freshness.
        original_command(deferred[0])
        time.sleep(0.02)
        assert controller.status == stopped
        with pytest.raises(LaserCancelledError):
            controller.arm(expected_stop_generation=generation)
    finally:
        worker.join(0.3)


def test_event_for_processed_pending_command_advances_received_sequence_floor(make_controller):
    controller, peer = make_controller()
    prepare(controller)
    controller.arm()
    previous = controller.status
    peer.hold.add("KEEP")
    errors = []

    def keep():
        try:
            controller.keepalive()
        except LaserError as error:
            errors.append(error)

    worker = threading.Thread(target=keep)
    worker.start()
    try:
        assert peer.hold_event.wait(0.3)
        assert peer.seq == previous.sequence + 1
        peer.io = False
        peer.advance(1)
        worker.join(0.3)
        assert not worker.is_alive() and errors
        assert controller.connected and controller.status.fault == "INTERLOCK"
        assert controller.status.sequence == peer.seq
        peer.inject(peer.held[0])  # Earlier report for cancelled KEEP is ignored.
        time.sleep(0.02)
        assert controller.status.fault == "INTERLOCK"
        # Even with a NEW report_id, sequence may not move behind an accepted
        # EVENT. A lower bound based only on the last ACK would miss this.
        peer.report("EVENT", previous.sequence, False, kind="EVENT")
        eventually(lambda: not controller.connected)
        assert controller.status.state is LaserState.UNKNOWN
    finally:
        worker.join(0.3)


@pytest.mark.parametrize("invalid", ["below_ack", "above_sent", "replayed_report", "wrong_session", "wrong_boot"])
def test_fault_event_keeps_nonce_report_and_sequence_replay_boundaries(make_controller, invalid):
    controller, peer = make_controller()
    prepare(controller)
    controller.arm()
    previous = controller.status
    peer.dark("INTERLOCK")
    peer.io = False

    def corrupt(wire):
        fields = wire.strip().split()
        if invalid == "below_ack": fields[4] = str(previous.sequence - 1)
        elif invalid == "above_sent": fields[4] = str(previous.sequence + 1)
        elif invalid == "replayed_report": fields[7] = str(previous.report_id)
        elif invalid == "wrong_session": fields[3] = "0" * 32
        else: fields[2] = "00000002"
        return " ".join(fields) + "\n"

    peer.mutate = corrupt
    peer.report("EVENT", peer.seq, False, kind="EVENT")
    eventually(lambda: not controller.connected)
    assert controller.status.state is LaserState.UNKNOWN
    assert controller.status.comp_high is None


def test_follow_rejects_cached_active_trigger_without_sending_follow(make_controller):
    controller, peer = make_controller()
    prepare(controller)
    controller.arm()
    peer.trigger = True
    # Generic readiness can observe active trigger; only entry to FOLLOW
    # requires inactive. Existing FOLLOW polling must still allow trigger HIGH.
    ready = controller.check_ready()
    assert ready.state is LaserState.ARMED and ready.trigger_active
    assert ready.interlock_ok and not ready.comp_high
    previous = list(peer.commands)
    with pytest.raises(LaserError, match="inactive"):
        controller.follow(1000)
    assert peer.commands == previous
    assert peer.state == "ARMED" and not peer.comp


def test_firmware_peer_rejects_trigger_activated_after_host_readiness(make_controller):
    controller, peer = make_controller()
    prepare(controller)
    controller.arm()
    ready = controller.check_ready()
    assert ready.interlock_ok and not ready.trigger_active
    peer.trigger = True  # Changed after the latest ACK, before FOLLOW acceptance.
    with pytest.raises(LaserError, match="TRIGGER_ACTIVE"):
        controller.follow(1000)
    assert "FOLLOW" in peer.commands
    assert controller.connected
    assert controller.status.state is LaserState.FAULT
    assert controller.status.fault == "TRIGGER_ACTIVE"
    assert not controller.status.comp_high and not controller.status.startup_checked
    assert peer.state == "FAULT" and not peer.comp


def test_follow_enters_dark_then_accepts_a_subsequent_trigger_edge(make_controller):
    controller, peer = make_controller()
    generation = prepare(controller)
    controller.arm(expected_stop_generation=generation)
    accepted = controller.follow(1000, expected_stop_generation=generation)
    assert accepted.state is LaserState.FOLLOW and not accepted.comp_high
    peer.trigger = True
    peer.advance(1)
    assert controller.keepalive(expected_stop_generation=generation).comp_high
    assert controller.check_ready().trigger_active
    controller.off()
    assert not peer.comp
