from dataclasses import replace
from enum import Enum
from types import SimpleNamespace
import threading
import time

import pytest

from automatic_transfer_station.axis_calibration import AXIS_CALIBRATIONS
from automatic_transfer_station.cutting_live import LiveQualification, LiveCuttingRunner, plan_live_line
from automatic_transfer_station.cutting_session import CuttingError, axis_configuration
from automatic_transfer_station.gas_controller import CuttingGateOutput
from test_cutting_session import Motion, reference


State = Enum('State', 'DISARMED ARMED FOLLOW FAULT')


class Laser:
    def __init__(self):
        self.status = SimpleNamespace(state=State.DISARMED, interlock_wired=True,
            interlock_ok=True, trigger_wired=True, watchdog_ms=500, comp_high=False,
            stop_generation=0, startup_checked=True, trigger_active=False)
        self.calls = []
        self.off_seen = threading.Event()
        self.fail_off = False

    def check_ready(self, require_trigger=True):
        self.calls.append('health')
        return self.status

    def off(self):
        self.calls.append('off')
        if self.fail_off:
            raise RuntimeError('serial disconnected')
        self.status.state = State.DISARMED
        self.status.comp_high = False
        self.status.stop_generation += 1
        return SimpleNamespace(**vars(self.status))

    def emergency_off(self):
        self.calls.append('emergency_off')
        self.off_seen.set()
        return self.off()

    def _check_generation(self, generation):
        if generation != self.status.stop_generation:
            raise CuttingError('old job must not resume')

    def arm(self, *, expected_stop_generation):
        self._check_generation(expected_stop_generation)
        self.calls.append('arm')
        self.status.state = State.ARMED

    def follow(self, window_ms, *, expected_stop_generation):
        self._check_generation(expected_stop_generation)
        self.calls.append(('follow', window_ms))
        self.status.state = State.FOLLOW

    def keepalive(self, *, expected_stop_generation):
        self._check_generation(expected_stop_generation)
        self.calls.append('keepalive')


class LiveMotion(Motion):
    def __init__(self):
        super().__init__()
        self.axis_calibrations = dict(AXIS_CALIBRATIONS)
        self.cutting_qualified = True
        self.qualified_cutting_gate = CuttingGateOutput(0, 7, 12, False)
        self.stopped = False
        self.block = None
        self.entered = threading.Event()

    def prepare_cutting_line(self, owner, start, end, out, speed, output):
        assert self.owner is owner
        self.calls.append(('prepare', start, end, out, speed, output))
        self.pending = out

    def start_prepared_cutting_line(self, owner):
        assert self.owner is owner
        self.calls.append('start_live')
        self.entered.set()
        if self.block:
            self.block.wait(2)
        self.pose = self.pending
        self.motion_revision += 1

    def stop_cutting(self, owner):
        self.stopped = True
        super().stop_cutting(owner)

    def get_cutting_lease_status(self, owner):
        return SimpleNamespace(valid=self.owner is owner,
            stop_confirmed=self.stopped, cleanup_required=False, can_retry_stop=False)


@pytest.fixture
def live(reference):
    _, snapshot, plan = reference
    motion, laser = LiveMotion(), Laser()
    qualification = LiveQualification('bench-record-1', axis_configuration(motion),
        motion.qualified_cutting_gate, True, True, True, 1., 1., 10., 1000., 1000, 60000)
    return motion, laser, snapshot, plan, qualification


def test_qualification_roundtrip_and_empty_template_cannot_qualify(live):
    motion, _, _, _, q = live
    assert LiveQualification.from_dict(q.to_dict()) == q
    with pytest.raises((TypeError, ValueError)):
        LiveQualification.from_dict(LiveQualification.template(motion))
    with pytest.raises(CuttingError, match='Independent'):
        replace(q, independent_inhibit_verified=False).require_qualified(motion)


@pytest.mark.parametrize('field,value', [('max_gate_latency_ms', float('nan')),
    ('max_path_error_um', 0), ('max_speed_um_s', 5001), ('max_window_ms', 60001),
    ('max_loss_exposure_ms', True), ('lead_distance_um', -1), ('timing_verified', 'yes')])
def test_malformed_qualification_rejected(live, field, value):
    with pytest.raises(ValueError):
        replace(live[-1], **{field: value})


def test_line_has_dark_acceleration_and_backlash_in_actual_stage_direction(live):
    motion, _, _, plan, q = live
    path = plan_live_line(plan, q, motion, 500)
    assert path.cut_start == plan.segments[-1].start_um
    assert path.cut_end == plan.segments[-1].end_um
    import math
    assert math.dist(path.lead_in, path.cut_start) == pytest.approx(10)
    assert math.dist(path.cut_end, path.lead_out) == pytest.approx(10)
    for pre, lead, a, b, backlash in zip(path.preload, path.lead_in, path.cut_start, path.cut_end, plan.backlash.effective_um):
        assert lead-pre == pytest.approx(math.copysign(backlash, b-a))
    assert 1 <= path.window_ms <= q.max_window_ms


@pytest.mark.parametrize('change', ['short_lead', 'latency', 'duration', 'scale', 'watchdog'])
def test_unqualified_motion_timing_rejected_before_hardware(live, change):
    motion, _, _, plan, q = live
    watchdog = 500
    if change == 'short_lead': q = replace(q, lead_distance_um=.1)
    if change == 'latency': q = replace(q, max_path_error_um=.001)
    if change == 'duration': q = replace(q, max_loss_exposure_ms=1, max_window_ms=1)
    if change == 'scale': motion.axis_calibrations[6] = replace(motion.axis_calibrations[6], pulses_per_unit=100000)
    if change == 'watchdog': watchdog = -1
    with pytest.raises(CuttingError): plan_live_line(plan, q, motion, watchdog)
    assert not motion.calls


def test_live_job_uses_board_prepared_trigger_and_disarms_after_completion(live):
    motion, laser, snap, plan, q = live
    LiveCuttingRunner(motion, laser).run(plan, snap, snap.context, q,
        calibration=plan.calibration, startup_confirmed=True)
    assert motion.calls[-1] == 'release'
    assert 'start_live' in motion.calls
    assert laser.calls.index('off') < laser.calls.index('arm')
    assert any(isinstance(c, tuple) and c[0] == 'follow' for c in laser.calls)
    assert laser.calls[-1] == 'off'


@pytest.mark.parametrize('missing', ['interlock', 'trigger', 'qualification', 'startup', 'polarity', 'watchdog'])
def test_live_is_disabled_with_current_arduino_only_hardware(live, missing):
    motion, laser, snap, plan, q = live
    startup = True
    if missing == 'interlock': laser.status.interlock_wired = False
    if missing == 'trigger': laser.status.trigger_wired = False
    if missing == 'qualification': q = replace(q, timing_verified=False)
    if missing == 'startup': startup = False
    if missing == 'polarity':
        q = replace(q, output=replace(q.output, active_high=True))
        motion.qualified_cutting_gate = q.output
    if missing == 'watchdog': laser.status.watchdog_ms = 1001
    with pytest.raises(CuttingError):
        LiveCuttingRunner(motion, laser).run(plan, snap, snap.context, q,
            calibration=plan.calibration, startup_confirmed=startup)
    assert not motion.calls and 'arm' not in laser.calls


def test_unsafe_dark_lead_blocks_all_motion_and_arming(live):
    motion, laser, snap, plan, q = live
    motion.invalid = True
    with pytest.raises(CuttingError, match='unsafe preload'):
        LiveCuttingRunner(motion, laser).run(plan, snap, snap.context, q,
            calibration=plan.calibration, startup_confirmed=True)
    assert 'arm' not in laser.calls
    assert not any(isinstance(c, tuple) and c[0] == 'move' for c in motion.calls)


def test_serial_failure_never_starts_motion(live):
    motion, laser, snap, plan, q = live
    laser.fail_off = True
    with pytest.raises(CuttingError, match='OFF unconfirmed'):
        LiveCuttingRunner(motion, laser).run(plan, snap, snap.context, q,
            calibration=plan.calibration, startup_confirmed=True)
    assert not any(isinstance(c, tuple) and c[0] == 'move' for c in motion.calls)
    assert motion.stopped


def test_off_is_independent_of_blocked_motion_dll_and_keepalives_do_not_continue(live):
    motion, laser, snap, plan, q = live
    motion.block = threading.Event()
    runner = LiveCuttingRunner(motion, laser)
    errors = []
    def run():
        try:
            runner.run(plan, snap, snap.context, q, calibration=plan.calibration, startup_confirmed=True)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert motion.entered.wait(1)
        before = laser.calls.count('keepalive')
        time.sleep(.1)
        assert laser.calls.count('keepalive') == before
        runner.abort()
        assert laser.off_seen.wait(.5)
    finally:
        motion.block.set()
        thread.join(2)
    assert not thread.is_alive() and errors


def test_stop_between_prepare_and_arm_cannot_rearm_old_job(live):
    motion, laser, snap, plan, q = live
    prepare = motion.prepare_cutting_line
    def race(*args):
        prepare(*args)
        laser.off()
    motion.prepare_cutting_line = race
    with pytest.raises(CuttingError, match='old job'):
        LiveCuttingRunner(motion, laser).run(plan, snap, snap.context, q,
            calibration=plan.calibration, startup_confirmed=True)
    assert 'arm' not in laser.calls and 'start_live' not in motion.calls


def test_runner_with_real_driver_and_byte_protocol_peer(live):
    from automatic_transfer_station.laser_controller import LaserController, LaserState
    from test_laser_controller import FakeSerial
    motion, _, snap, plan, q = live
    peer = FakeSerial()
    driver = LaserController(serial_factory=lambda **_: peer)
    driver.connect('FAKE_ONLY')
    try:
        driver.off()
        driver.confirm_startup_check()
        LiveCuttingRunner(motion, driver).run(plan, snap, snap.context, q,
            calibration=plan.calibration, startup_confirmed=True)
        assert driver.status.state is LaserState.DISARMED and not peer.comp
        assert 'FOLLOW' in peer.commands and 'start_live' in motion.calls
    finally:
        driver.disconnect()


def test_active_trigger_after_board_prepare_prevents_arm_and_start(live):
    motion, laser, snap, plan, q = live
    prepare = motion.prepare_cutting_line
    def stuck(*args):
        prepare(*args)
        laser.status.trigger_active = True
    motion.prepare_cutting_line = stuck
    with pytest.raises(CuttingError, match='trigger must be inactive'):
        LiveCuttingRunner(motion, laser).run(plan, snap, snap.context, q,
            calibration=plan.calibration, startup_confirmed=True)
    assert 'arm' not in laser.calls and 'start_live' not in motion.calls


def test_exact_theoretical_lead_is_rejected_before_quantized_board_travel(live):
    motion, _, _, plan, q = live
    from automatic_transfer_station.gas_controller import CUTTING_ACCELERATION_PULSE_MS2
    acceleration = CUTTING_ACCELERATION_PULSE_MS2 * 1e9 / motion.axis_calibrations[5].pulses_per_unit
    q = replace(q, lead_distance_um=plan.speed_um_s ** 2 / (2 * acceleration))
    with pytest.raises(CuttingError, match='lead distance'):
        plan_live_line(plan, q, motion, 500)


@pytest.mark.parametrize('stop_command', ['off', 'emergency_off'])
@pytest.mark.parametrize('phase', ['last_motion_query', 'final_off'])
def test_external_stop_at_completion_cannot_be_reported_as_success(live, stop_command, phase):
    from automatic_transfer_station.laser_controller import LaserController
    from test_laser_controller import FakeSerial

    motion, _, snapshot, plan, qualification = live
    peer = FakeSerial()
    driver = LaserController(serial_factory=lambda **_: peer)
    driver.connect('FAKE_ONLY')
    try:
        driver.off()
        driver.confirm_startup_check()
        original_status, original_off = motion.cutting_move_status, driver.off
        stop = original_off if stop_command == 'off' else driver.emergency_off
        injected = False

        def inject():
            nonlocal injected
            if 'start_live' in motion.calls and not injected:
                injected = True
                stop()

        def read_motion(owner):
            result = original_status(owner)
            if phase == 'last_motion_query' and result:
                inject()
            return result

        def final_off():
            if phase == 'final_off':
                inject()
            return original_off()

        motion.cutting_move_status, driver.off = read_motion, final_off
        progress = []
        runner = LiveCuttingRunner(motion, driver)
        with pytest.raises(CuttingError, match='stop generation'):
            runner.run(plan, snapshot, snapshot.context, qualification,
                       calibration=plan.calibration, startup_confirmed=True,
                       progress=lambda *p: progress.append(p))
        assert injected
        assert (3, 3) not in progress
        assert motion.stopped and motion.owner is None
        assert not peer.comp
        assert peer.commands.count('FOLLOW') == 1
    finally:
        driver.disconnect()


@pytest.mark.parametrize('end', [(60., 50.), (40., 50.), (50., 60.), (50., 40.)])
def test_fitted_roundoff_does_not_add_stationary_axis_live_backlash(live, end):
    from automatic_transfer_station.cutting_geometry import (
        BacklashSettings, CalibrationSample, ROI, fit_calibration, plan_line,
    )

    motion, _, snapshot, old_plan, qualification = live
    poses = ((0, 0), (3, 7), (9, 4), (8, 9))
    calibration = fit_calibration(
        [CalibrationSample((x, y), (30 + x, 30 + y)) for x, y in poses],
        (50, 50), old_plan.context, .1)
    plan = plan_line((50, 50), end, snapshot.stage_um, calibration,
                     ROI(0, 0, 100, 100), BacklashSettings(2, 3), 100)
    path = plan_live_line(plan, qualification, motion, 500)
    moving = 0 if end[0] != 50 else 1
    stationary = 1 - moving
    assert plan.preload_um[stationary] == 0
    assert path.preload[stationary] == path.lead_in[stationary]
    assert abs(path.lead_in[moving] - path.preload[moving]) == (2, 3)[moving]


def test_single_live_quantized_endpoint_outside_roi_rejected_before_motion(live):
    from automatic_transfer_station.cutting_geometry import BacklashSettings, ROI, plan_line

    motion, laser, snapshot, old_plan, qualification = live
    plan = plan_line((90., 50.), (99.995, 50.), snapshot.stage_um,
                     old_plan.calibration, ROI(0, 0, 100, 100), BacklashSettings(), 100)
    with pytest.raises(CuttingError, match='Quantized.*ROI'):
        LiveCuttingRunner(motion, laser).run(plan, snapshot, snapshot.context, qualification,
            calibration=plan.calibration, startup_confirmed=True)
    assert not any(isinstance(call, tuple) and call[0] == 'move' for call in motion.calls)
    assert 'arm' not in laser.calls and 'start_live' not in motion.calls


def test_live_preview_accepts_calibration_values_without_controller_methods(live):
    motion, _, _, plan, qualification = live
    proxy = SimpleNamespace(axis_calibrations={
        axis: SimpleNamespace(**vars(calibration))
        for axis, calibration in motion.axis_calibrations.items()
    })
    expected = plan_live_line(plan, qualification, motion, 500)
    assert plan_live_line(plan, qualification, proxy, 500) == expected
    assert motion.calls == []
