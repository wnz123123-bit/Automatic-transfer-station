"""Raster execution against a real controller + injected board/serial fakes only."""
from dataclasses import FrozenInstanceError, asdict, replace
import math
import threading
import time
from types import SimpleNamespace

import pytest

from automatic_transfer_station.camera_controller import CameraFrame
from automatic_transfer_station.controller_base import MotionControllerError
from automatic_transfer_station.cutting_geometry import BacklashSettings, CalibrationContext, CuttingCalibration, ROI
from automatic_transfer_station.cutting_live import LiveQualification
from automatic_transfer_station.cutting_raster import DotExposure, RasterMove, RasterSettings, plan_raster
from automatic_transfer_station.cutting_raster_runner import RasterLiveLine, RasterRunner, build_raster_route
from automatic_transfer_station.cutting_session import CuttingError, CuttingSnapshot, FrameEnvelope, axis_configuration
from automatic_transfer_station.gas_controller import CuttingGateOutput, GasMotionController
from automatic_transfer_station.laser_controller import LaserState, LaserStatus
from test_cutting_live_motion import LiveApi


class Board(LiveApi):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.block = None
        self.on_start = lambda: None

    def crd_start(self, *args):
        result = super().crd_start(*args)
        self.started.set()
        self.on_start()
        if self.block:
            assert self.block.wait(2), "test failed to release fake DLL"
        return result

    def crd_status(self, *args):
        if self.running:
            self.finish()
        return super().crd_status(*args)


class Laser:
    """Explicit receipt fake: completion source is independent of DISARMED state."""
    connected = True

    def __init__(self):
        self.status = LaserStatus(state=LaserState.DISARMED, interlock_wired=True,
            interlock_ok=True, trigger_wired=True, trigger_active=False, comp_high=False,
            watchdog_ms=500, max_window_ms=60000, startup_checked=True,
            boot_nonce="00000001", session_nonce="a" * 32)
        self.calls = []
        self.completion = None
        self.receipt = None
        self.finish_mode = "natural"
        self.fail_off = False
        self.complete_inside_fire = False
        self.polls_until_done = 1
        self.fire_polls = 0
        self.off_seen = threading.Event()
        self.on_fire = lambda: None

    def _command(self, name, generation=None):
        if generation is not None and generation != self.status.stop_generation:
            raise CuttingError("old job cannot resume")
        self.calls.append(name)
        self.status = replace(self.status, sequence=self.status.sequence + 1,
                              report_id=self.status.report_id + 1)

    def check_ready(self, require_trigger=True):
        return self.poll()

    def poll(self):
        self._command("poll")
        if self.status.state is LaserState.FIRING:
            self.fire_polls += 1
            if self.fire_polls >= self.polls_until_done:
                self.complete()
        return self.status

    def off(self):
        self._command("off")
        if self.fail_off:
            raise RuntimeError("serial OFF transport failure")
        self.status = replace(self.status, state=LaserState.DISARMED, comp_high=False,
                              window_remaining_ms=0, fault=None,
                              stop_generation=self.status.stop_generation + 1)
        return self.status

    def emergency_off(self):
        self.off_seen.set()
        result = self.off()
        self.calls.append("emergency_off")
        return result

    def arm(self, *, expected_stop_generation):
        self._command("arm", expected_stop_generation)
        assert self.status.state is LaserState.DISARMED
        self.status = replace(self.status, state=LaserState.ARMED)
        return self.status

    def keepalive(self, *, expected_stop_generation):
        self._command("keep", expected_stop_generation)
        assert self.status.state in (LaserState.ARMED, LaserState.FIRING, LaserState.FOLLOW), "KEEP after completion"
        return self.status

    def follow(self, duration, *, expected_stop_generation):
        self._command("follow", expected_stop_generation)
        self.status = replace(self.status, state=LaserState.FOLLOW, window_remaining_ms=duration)
        return self.status

    def fire(self, duration, *, expected_stop_generation):
        # An in-flight UI STATUS consumed some sequences. No caller may predict.
        self.status = replace(self.status, sequence=self.status.sequence + 7)
        self._command("fire", expected_stop_generation)
        self.status = replace(self.status, state=LaserState.FIRING, comp_high=True,
                              window_remaining_ms=duration)
        self.receipt = SimpleNamespace(fire_sequence=self.status.sequence,
            started_generation=expected_stop_generation,
            boot_nonce=self.status.boot_nonce, session_nonce=self.status.session_nonce)
        self.fire_polls = 0
        self.completion = None
        self.on_fire()
        if self.complete_inside_fire:
            self.complete()
        return SimpleNamespace(**{**asdict(self.status), "fire_token": self.receipt})

    def complete(self):
        mode = self.finish_mode
        self.status = replace(self.status, state=LaserState.FAULT if mode == "fault" else LaserState.DISARMED,
            comp_high=False, window_remaining_ms=0, fault="WATCHDOG" if mode == "fault" else None,
            stop_generation=self.status.stop_generation + 1)
        if mode != "off" and mode != "fault":
            self.completion = SimpleNamespace(**vars(self.receipt), completed_generation=self.status.stop_generation)
            if mode == "wrong_sequence":
                self.completion.fire_sequence += 1
            if mode == "wrong_session":
                self.completion.session_nonce = "b" * 32
            if mode == "wrong_generation":
                self.completion.started_generation -= 1

    def get_fire_completion(self, receipt):
        assert receipt is self.receipt
        return self.completion


@pytest.fixture
def rig():
    api = Board()
    controller = GasMotionController(api, cutting_qualified=True)
    controller.is_open = True  # Test injection only; GA_Open is never called.
    output = CuttingGateOutput(3, 7, 42, False)  # Arbitrary fake mapping, not a wiring recommendation.
    controller.configure_cutting_gate(output, qualified=True)
    context = CalibrationContext("fake-camera", "20X", 100, 100, 1)
    calibration = CuttingCalibration(((1., 0.), (0., 1.)), (0., 0.), (50., 50.), context, 0., 0., .1, 3, "test-fit")
    snapshot = CuttingSnapshot(FrameEnvelope(CameraFrame(100, 100, bytes(30000)), context, time.monotonic()),
        tuple(controller.get_cutting_reference().items()), controller.get_cutting_pose(),
        controller.motion_revision, axis_configuration(controller))
    qualification = LiveQualification("fake-bench", axis_configuration(controller), output,
        True, True, True, 1., 1., 10., 1000., 1000, 60000)
    return SimpleNamespace(c=controller, api=api, laser=Laser(), cal=calibration,
                           snapshot=snapshot, q=qualification)


def plan_for(rig, mode="lines", *, polygon=None, **settings):
    defaults = dict(mode=mode, row_pitch_um=5., speed_um_s=100.)
    if mode == "dots":
        defaults.update(point_pitch_um=5., settle_ms=0, dwell_ms=1)
    defaults.update(settings)
    return plan_raster(polygon or ((30., 30.), (40., 30.), (40., 40.), (30., 40.)),
        rig.snapshot.stage_um, rig.cal, ROI(0, 0, 100, 100), BacklashSettings(2, 3), RasterSettings(**defaults))


def run(rig, plan, *, live=False, runner=None, **kwargs):
    runner = runner or RasterRunner(rig.c, rig.laser)
    runner.run(plan, rig.snapshot, rig.snapshot.context, calibration=rig.cal,
               qualification=rig.q if live else None, startup_confirmed=live, **kwargs)
    return runner


def launched(api):
    return [call for call in api.calls if call[0] == "crd_start"]


@pytest.fixture(params=["raster", "single_live"])
def lifecycle_job(rig, request):
    """Both workers use real controller leases with fake board/laser IO only."""
    if request.param == "raster":
        runner = RasterRunner(rig.c, rig.laser)
        plan = plan_for(rig)
        def execute(**kwargs):
            return run(rig, plan, runner=runner, live=True, **kwargs)
    else:
        from automatic_transfer_station.cutting_geometry import plan_line
        from automatic_transfer_station.cutting_live import LiveCuttingRunner
        runner = LiveCuttingRunner(rig.c, rig.laser)
        plan = plan_line((30., 30.), (40., 30.), rig.snapshot.stage_um,
                         rig.cal, ROI(0, 0, 100, 100), BacklashSettings(2, 3), 100.)
        def execute(**kwargs):
            return runner.run(plan, rig.snapshot, rig.snapshot.context, rig.q,
                              calibration=rig.cal, startup_confirmed=True, **kwargs)
    return runner, execute


def test_terminal_success_closes_abort_before_released_lease_can_be_reused(rig, lifecycle_job):
    runner, execute = lifecycle_job
    released, finish_release, old_off, finish_off = (threading.Event() for _ in range(4))
    original_release, original_off = rig.c.release_cutting, rig.laser.emergency_off
    errors = []
    def release(owner):
        original_release(owner)
        released.set()
        assert finish_release.wait(3), "test did not finish release"
    def delayed_off():
        old_off.set()
        assert finish_off.wait(3), "test did not finish old OFF"
        return original_off()
    def work():
        try:
            execute()
        except BaseException as error:
            errors.append(error)
    rig.c.release_cutting, rig.laser.emergency_off = release, delayed_off
    worker = threading.Thread(target=work)
    next_owner = object()
    worker.start()
    try:
        assert released.wait(2)
        rig.c.acquire_cutting(next_owner)
        rig.laser.arm(expected_stop_generation=rig.laser.status.stop_generation)
        before = (len(rig.laser.calls), len(rig.api.calls))
        runner.abort()  # Old run is still inside release, a new job owns motion.
        assert not runner.cancel.is_set()
        assert not runner._abort_tasks and not old_off.is_set()
        finish_release.set()
        worker.join(2)
        assert not worker.is_alive() and not errors
        runner.abort()  # Also inert after run has returned.
        assert before == (len(rig.laser.calls), len(rig.api.calls))
        assert rig.c.get_cutting_lease_status(next_owner).valid
        assert rig.laser.status.state is LaserState.ARMED
    finally:
        finish_release.set()
        finish_off.set()
        worker.join(3)
        for task in getattr(runner, "_abort_tasks", ()):
            task.join(2)
        if rig.c.get_cutting_lease_status(next_owner).valid:
            original_release(next_owner)


def test_abort_wins_terminal_and_pending_off_blocks_next_job_until_retry(rig, lifecycle_job):
    runner, execute = lifecycle_job
    at_terminal, finish_progress, old_off, finish_off = (threading.Event() for _ in range(4))
    original_off = rig.laser.emergency_off
    errors = []
    def progress(done, total):
        if done == total:
            at_terminal.set()
            assert finish_progress.wait(3), "test did not finish progress"
    def delayed_off():
        if threading.current_thread() is not worker:
            old_off.set()
            assert finish_off.wait(3), "test did not finish abort OFF"
        return original_off()
    def work():
        try:
            execute(progress=progress)
        except BaseException as error:
            errors.append(error)
    rig.laser.emergency_off = delayed_off
    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert at_terminal.wait(2)
        runner.abort()
        assert old_off.wait(1)
        finish_progress.set()
        worker.join(2)
        assert not worker.is_alive() and errors
        assert "cancelled" in str(errors[0]).lower()
        assert runner.cleanup_required and "abort worker" in runner.cleanup_error
        with pytest.raises(MotionControllerError, match="lease"):
            rig.c.acquire_cutting(object())
        with pytest.raises(MotionControllerError, match="lease"):
            rig.c.start_jog(1, 1)
        finish_off.set()
        for task in runner._abort_tasks:
            task.join(1)
        runner.retry_cleanup()
        assert not runner.cleanup_required and runner.cleanup_error is None
        next_owner = object()
        rig.c.acquire_cutting(next_owner)
        rig.laser.arm(expected_stop_generation=rig.laser.status.stop_generation)
        before = len(rig.laser.calls)
        runner.abort()
        runner.retry_cleanup()
        assert len(rig.laser.calls) == before
        assert rig.laser.status.state is LaserState.ARMED
        rig.c.release_cutting(next_owner)
    finally:
        finish_progress.set()
        finish_off.set()
        worker.join(3)
        for task in getattr(runner, "_abort_tasks", ()):
            task.join(2)


@pytest.mark.parametrize("failure", ["serial", "motion"])
def test_release_failure_after_terminal_seal_keeps_cleanup_interlock(rig, lifecycle_job, failure):
    runner, execute = lifecycle_job
    def fail_release(owner):
        if failure == "serial":
            rig.laser.fail_off = True
        else:
            rig.api.failures["stop"] = -7
        raise CuttingError("terminal release failed")
    rig.c.release_cutting = fail_release
    with pytest.raises(CuttingError, match="terminal release failed"):
        execute()
    assert runner.cleanup_required
    with pytest.raises(MotionControllerError, match="lease"):
        rig.c.acquire_cutting(object())
    before = len(rig.laser.calls)
    runner.abort()
    assert len(rig.laser.calls) == before
    rig.laser.fail_off = False
    rig.api.failures.clear()
    runner.retry_cleanup()
    assert not runner.cleanup_required


def test_failed_lease_acquisition_and_late_abort_cannot_off_existing_job(rig, lifecycle_job):
    runner, execute = lifecycle_job
    next_owner = object()
    rig.c.acquire_cutting(next_owner)
    rig.laser.arm(expected_stop_generation=rig.laser.status.stop_generation)
    with pytest.raises(MotionControllerError, match="lease"):
        execute()
    before = len(rig.laser.calls)
    runner.abort()
    assert len(rig.laser.calls) == before
    assert rig.laser.status.state is LaserState.ARMED
    assert rig.c.get_cutting_lease_status(next_owner).valid
    assert not runner.cleanup_required
    rig.c.release_cutting(next_owner)


@pytest.mark.parametrize("new_owner", [False, True])
def test_failed_cleanup_global_stop_retry_respects_exact_owner(rig, lifecycle_job, new_owner):
    runner, execute = lifecycle_job
    rig.laser.fail_off = True
    with pytest.raises(CuttingError, match="OFF unconfirmed"):
        execute()
    assert runner.cleanup_required
    rig.c.stop_all()
    rig.laser.fail_off = False
    owner = object()
    if new_owner:
        rig.c.acquire_cutting(owner)
        rig.laser.arm(expected_stop_generation=rig.laser.status.stop_generation)
        before = (len(rig.laser.calls), len(rig.api.calls))
        with pytest.raises(CuttingError, match="stale recovery owner"):
            runner.retry_cleanup()
        assert before == (len(rig.laser.calls), len(rig.api.calls))
        assert runner.cleanup_required and rig.c.get_cutting_lease_status(owner).valid
        assert rig.laser.status.state is LaserState.ARMED
    else:
        runner.retry_cleanup()
        assert not runner.cleanup_required
        rig.c.acquire_cutting(owner)
    rig.c.release_cutting(owner)


def test_failure_cleanup_closes_abort_admission_before_waiting_for_off(rig, lifecycle_job):
    runner, execute = lifecycle_job
    cleaning, finish_off = threading.Event(), threading.Event()
    original_off = rig.laser.emergency_off
    errors = []
    def fail_guard():
        if rig.c.get_cutting_lease_status(runner.owner).valid:
            raise CuttingError("preflight failed while leased")
    def delayed_off():
        cleaning.set()
        assert finish_off.wait(3), "test did not finish cleanup OFF"
        return original_off()
    def work():
        try:
            execute(reference_guard=fail_guard)
        except BaseException as error:
            errors.append(error)
    rig.laser.emergency_off = delayed_off
    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert cleaning.wait(2)
        runner.abort()
        assert not runner._abort_tasks
        finish_off.set()
        worker.join(2)
        assert not worker.is_alive() and len(errors) == 1
        assert "preflight failed" in str(errors[0])
        assert not runner.cleanup_required
    finally:
        finish_off.set()
        worker.join(3)


@pytest.mark.parametrize("mode", ["lines", "dots"])
def test_builder_pure_immutable_and_preview_comes_from_final_actions(rig, mode):
    class Proxy:
        axis_calibrations = rig.c.axis_calibrations
        def __getattr__(self, name):
            raise AssertionError(f"Builder touched device API: {name}")
    plan = plan_for(rig, mode)
    route = build_raster_route(plan, controller=Proxy())
    route.verify_integrity()
    assert route.actions == plan.actions
    assert route.preview_segments == tuple((a.start_um, a.end_um, a.cut) for a in plan.actions if isinstance(a, RasterMove))
    assert route.exposure_points == tuple(a.point_um for a in plan.actions if isinstance(a, DotExposure))
    with pytest.raises(FrozenInstanceError):
        route.actions = ()


@pytest.mark.parametrize("field,value", [("actions", ()), ("path_endpoints_um", ()),
    ("preview_segments", ()), ("exposure_points", ((1., 2.),)), ("estimated_duration_s", .0001),
    ("exposure_count", 0)])
def test_route_tampering_rejected_before_first_motion(rig, field, value):
    plan = plan_for(rig)
    route = replace(build_raster_route(plan, controller=rig.c), **{field: value})
    with pytest.raises(CuttingError, match="provenance"):
        run(rig, plan, route=route)
    assert not launched(rig.api)


def test_qualified_routes_keep_original_snapshot_reference_for_every_row(rig):
    plan = plan_for(rig, angle_deg=90)
    route = build_raster_route(plan, rig.q, rig.c, 500)
    live = [a for a in route.actions if isinstance(a, RasterLiveLine)]
    assert len(live) == len(plan.lines) > 1
    for action, source in zip(live, plan.lines):
        assert action.path.reference == plan.reference_stage_um
        assert action.path.cut_start == pytest.approx(source.start_um)
        assert action.path.cut_end == pytest.approx(source.end_um)
    route.verify_integrity()


def test_quantized_short_oblique_row_rejected_before_dark_approach(rig):
    plan = plan_for(rig, angle_deg=27)
    with pytest.raises(CuttingError, match="collinear"):
        run(rig, plan, live=True)
    assert not launched(rig.api)


def test_live_route_dark_rehearsal_traverses_every_preview_endpoint_without_emission(rig):
    plan = plan_for(rig)
    route = build_raster_route(plan, rig.q, rig.c, 500)
    run(rig, plan, route=route)
    targets = [call[2:4] for call in rig.api.calls if call[0] == "line_xy"]
    expected = [tuple(rig.c.axis_calibrations[a].pulse_from_coordinate(v / 1000)
                      for a, v in zip((5, 6), p)) for p in route.path_endpoints_um[1:]]
    assert targets == expected
    assert not any(command in rig.laser.calls for command in ("arm", "fire", "follow", "keep"))
    assert not any(call[0] == "buf_io" for call in rig.api.calls)


@pytest.mark.parametrize("mode", ["lines", "dots"])
def test_one_lease_blocks_manual_axes_for_entire_job(rig, mode):
    progress = []
    plan = plan_for(rig, mode)
    runner = RasterRunner(rig.c, rig.laser)
    def observe(done, total):
        assert rig.c.get_cutting_lease_status(runner.owner).valid
        with pytest.raises(MotionControllerError, match="lease"):
            rig.c.start_jog(1, 1)
        progress.append((done, total))
    run(rig, plan, runner=runner, progress=observe)
    assert progress[-1] == (len(plan.actions), len(plan.actions))
    assert rig.c.get_cutting_lease_status(runner.owner).state == "released"


def test_live_lines_prepare_follow_start_and_off_per_row(rig):
    plan = plan_for(rig)
    states = []
    rig.api.on_start = lambda: states.append(rig.laser.status.state)
    run(rig, plan, live=True)
    assert states.count(LaserState.FOLLOW) == plan.exposure_count
    assert rig.laser.calls.count("follow") == plan.exposure_count
    assert "fire" not in rig.laser.calls
    assert rig.laser.status.state is LaserState.DISARMED
    assert not rig.c.get_cutting_lease_status(None).cleanup_required


@pytest.mark.parametrize("inside", [False, True])
def test_dot_actual_receipt_handles_monitor_sequences_and_natural_expiry(rig, inside):
    rig.laser.complete_inside_fire = inside
    plan = plan_for(rig, "dots")
    states = []
    rig.api.on_start = lambda: states.append(rig.laser.status.state)
    run(rig, plan, live=True)
    assert rig.laser.calls.count("fire") == plan.exposure_count
    assert all(state is LaserState.DISARMED for state in states)
    assert "follow" not in rig.laser.calls
    assert rig.laser.status.state is LaserState.DISARMED


@pytest.mark.parametrize("mode", ["off", "fault", "wrong_sequence", "wrong_session", "wrong_generation"])
def test_dot_never_mistakes_stop_fault_or_wrong_receipt_for_completion(rig, mode):
    rig.laser.finish_mode = mode
    with pytest.raises(CuttingError):
        run(rig, plan_for(rig, "dots"), live=True)
    assert rig.laser.calls.count("fire") == 1
    assert rig.laser.off_seen.is_set()


def test_missing_natural_completion_capability_fails_before_any_move(rig):
    rig.laser.get_fire_completion = None
    with pytest.raises(CuttingError, match="completion capability"):
        run(rig, plan_for(rig, "dots"), live=True)
    assert not launched(rig.api)


@pytest.mark.parametrize("mode", ["lines", "dots"])
def test_subpulse_exposures_fail_in_pure_builder_and_runtime_preflight(rig, mode):
    if mode == "dots":
        plan = plan_for(rig, mode, point_pitch_um=.005,
                        polygon=((39.95, 39.9), (40., 39.9), (40., 40.), (39.95, 40.)))
    else:
        plan = plan_for(rig, mode,
                        polygon=((39.999, 39.9), (40., 39.9), (40., 40.), (39.999, 40.)))
    with pytest.raises(CuttingError, match="pulse"):
        build_raster_route(plan, controller=rig.c)
    # An old base preview without pulse calibration is still rejected at run.
    route = build_raster_route(plan)
    with pytest.raises(CuttingError, match="pulse"):
        run(rig, plan, route=route)
    assert not launched(rig.api)


def test_every_added_lead_checked_before_first_motion(rig):
    plan = plan_for(rig)
    route = build_raster_route(plan, rig.q, rig.c, 500)
    seen = []
    def reject(owner, points):
        seen.extend(points)
        raise MotionControllerError("last lead-out violates safety bound")
    rig.c.validate_cutting_path = reject
    with pytest.raises(MotionControllerError, match="last lead"):
        run(rig, plan, live=True, route=route)
    assert tuple(seen) == route.path_endpoints_um
    assert not launched(rig.api) and "arm" not in rig.laser.calls


@pytest.mark.parametrize("limit", ["MAX_ACTIONS", "MAX_EXPOSURES", "MAX_JOB_SECONDS"])
def test_complete_route_resource_limits(rig, monkeypatch, limit):
    import automatic_transfer_station.cutting_raster_runner as module
    plan = plan_for(rig)
    monkeypatch.setattr(module, limit, 1)
    with pytest.raises(CuttingError):
        build_raster_route(plan, rig.q, rig.c, 500)


def test_camera_guard_change_stops_before_next_move(rig):
    plan = plan_for(rig)
    def guard():
        if launched(rig.api):
            raise CuttingError("camera mode changed")
    with pytest.raises(CuttingError, match="camera mode"):
        run(rig, plan, reference_guard=guard)
    assert len(launched(rig.api)) == 1


def test_serial_off_failure_retains_recovery_lease_and_global_stop_allows_retry(rig):
    runner = RasterRunner(rig.c, rig.laser)
    rig.laser.fail_off = True
    with pytest.raises(CuttingError, match="OFF unconfirmed"):
        run(rig, plan_for(rig, "dots"), live=True, runner=runner)
    assert runner.cleanup_required and runner.cleanup_error
    with pytest.raises(MotionControllerError, match="lease"):
        rig.c.start_jog(1, 1)
    rig.c.stop_all()
    rig.laser.fail_off = False
    runner.retry_cleanup()
    assert not runner.cleanup_required
    owner = object()
    rig.c.acquire_cutting(owner)
    calls = len(rig.api.calls)
    runner.abort()  # Completed/failed old runners never stop a future job.
    runner.retry_cleanup()
    assert len(rig.api.calls) == calls
    rig.c.release_cutting(owner)


def test_stop_cleanup_failure_is_not_released_or_masked(rig):
    runner = RasterRunner(rig.c, rig.laser)
    rig.api.failures["stop"] = -7
    def fail():
        raise CuttingError("primary failure")
    with pytest.raises(CuttingError, match="primary failure.*cleanup"):
        run(rig, plan_for(rig), runner=runner, reference_guard=lambda: fail() if rig.c.get_cutting_lease_status(runner.owner).valid else None)
    assert runner.cleanup_required and rig.c.get_cutting_lease_status(runner.owner).cleanup_required
    rig.api.failures.clear()
    runner.retry_cleanup()
    assert not runner.cleanup_required


def test_abort_off_does_not_wait_for_blocked_motion_dll(rig):
    plan = plan_for(rig)
    runner = RasterRunner(rig.c, rig.laser)
    rig.api.block = threading.Event()
    errors = []
    def work():
        try:
            run(rig, plan, live=True, runner=runner)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=work)
    thread.start()
    try:
        assert rig.api.started.wait(1)
        count = rig.laser.calls.count("keep")
        runner.abort()
        runner.abort()
        assert rig.laser.off_seen.wait(.5)
        assert rig.laser.calls.count("keep") == count
    finally:
        rig.api.block.set()
        thread.join(3)
    assert not thread.is_alive() and errors


def test_dark_run_without_any_laser_and_single_use_runner(rig):
    runner = RasterRunner(rig.c)
    run(rig, plan_for(rig), runner=runner)
    with pytest.raises(CuttingError, match="single-use"):
        run(rig, plan_for(rig), runner=runner)


def test_subpulse_row_pitch_cannot_expose_same_actual_line_twice(rig):
    plan = plan_for(rig, row_pitch_um=.005,
                    polygon=((30., 39.95), (40., 39.95), (40., 40.), (30., 40.)))
    with pytest.raises(CuttingError, match="same pulse line"):
        build_raster_route(plan, controller=rig.c)


@pytest.mark.parametrize("phase", ["arm", "keep"])
def test_abort_between_arm_and_fire_never_sends_fire(rig, phase):
    runner = RasterRunner(rig.c, rig.laser)
    name = "arm" if phase == "arm" else "keepalive"
    original = getattr(rig.laser, name)
    def cancel(**kwargs):
        result = original(**kwargs)
        runner.cancel.set()
        return result
    setattr(rig.laser, name, cancel)
    with pytest.raises(CuttingError, match="cancelled"):
        run(rig, plan_for(rig, "dots"), live=True, runner=runner)
    assert "fire" not in rig.laser.calls


class ClockEvent:
    """Virtual-time waits for timing invariants; not used in threaded tests."""
    def __init__(self, clock):
        self.clock, self.cancelled = clock, False
    def is_set(self):
        return self.cancelled
    def set(self):
        self.cancelled = True
    def wait(self, duration):
        self.clock.value += duration
        return self.cancelled


def test_settle_elapsed_before_fire_and_off_confirmed_before_next_move(rig, monkeypatch):
    import automatic_transfer_station.cutting_raster_runner as module
    clock = SimpleNamespace(value=0.)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    runner = RasterRunner(rig.c, rig.laser)
    runner.cancel = ClockEvent(clock)
    arrivals, fires = [], []
    def arrived():
        assert rig.laser.status.state is LaserState.DISARMED
        assert rig.laser.status.comp_high is False
        arrivals.append(clock.value)
    def fire():
        fires.append(clock.value)
        assert fires[-1] - arrivals[-1] >= .05 - 1e-12
    rig.api.on_start, rig.laser.on_fire = arrived, fire
    plan = plan_for(rig, "dots", settle_ms=50, dwell_ms=5)
    run(rig, plan, live=True, runner=runner)
    assert len(fires) == plan.exposure_count


def test_monotonic_whole_job_deadline_prevents_another_action(rig, monkeypatch):
    import automatic_transfer_station.cutting_raster_runner as module
    clock = SimpleNamespace(value=0.)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    def expire(*_):
        clock.value = 3601.
    with pytest.raises(CuttingError, match="job deadline"):
        run(rig, plan_for(rig), progress=expire)
    assert len(launched(rig.api)) == 1


@pytest.mark.parametrize("change", ["revision", "context", "axis", "route_profile", "startup"])
def test_stale_snapshot_or_live_profile_never_moves(rig, change):
    plan = plan_for(rig)
    route = build_raster_route(plan, rig.q, rig.c, 500)
    if change == "revision":
        rig.c._motion_revision += 1
    elif change == "context":
        rig.snapshot = replace(rig.snapshot, envelope=replace(rig.snapshot.envelope,
            context=replace(rig.snapshot.context, binning=2)))
    elif change == "axis":
        rig.api.positions[9] = rig.api.positions.get(9, 0) + 10
    elif change == "route_profile":
        route = build_raster_route(plan, replace(rig.q, evidence_id="different-bench"), rig.c, 500)
    else:
        rig.laser.status = replace(rig.laser.status, startup_checked=False)
    with pytest.raises((CuttingError, ValueError)):
        run(rig, plan, live=True, route=route)
    assert not launched(rig.api)


@pytest.mark.parametrize("completion", ["fire_ack", "fire_event", "poll", "keep", "bare_off", "watchdog"])
def test_real_laser_driver_byte_peer_fire_completion_races(rig, completion):
    """Host/protocol test, NOT an AVR/USB/UART dynamic timing simulation."""
    from automatic_transfer_station.laser_controller import LaserController
    from test_laser_controller import FakeSerial
    peer = FakeSerial()
    driver = LaserController(serial_factory=lambda **_: peer)
    driver.connect("FAKE_ONLY")
    try:
        driver.off()
        driver.confirm_startup_check()
        rig.laser = driver
        command, report = peer.command, peer.report

        def changed_report(verb, sequence, *args, **kwargs):
            if verb == "FIRE" and completion == "fire_ack":
                peer.now += peer.window
                peer.dark()
                peer.fire_seq, peer.completion_reason = sequence, "FIRE_DONE"
            report(verb, sequence, *args, **kwargs)
            if verb == "FIRE" and completion == "fire_event":
                peer.advance(peer.window)

        def changed_command(line):
            fields = line.split()
            verb = fields[4] if len(fields) >= 5 else None
            if peer.state == "FIRING" and (
                    verb == "STATUS" and completion in ("poll", "bare_off", "watchdog")
                    or verb == "KEEP" and completion == "keep"):
                if completion == "bare_off":
                    command("OFF")
                else:
                    peer.advance(501 if completion == "watchdog" else peer.window)
            command(line)

        peer.command, peer.report = changed_command, changed_report
        plan = plan_for(rig, "dots", dwell_ms=1000 if completion == "watchdog" else 10)
        if completion in ("bare_off", "watchdog"):
            with pytest.raises(Exception):
                run(rig, plan, live=True)
            assert peer.commands.count("FIRE") == 1
        else:
            run(rig, plan, live=True)
            assert peer.commands.count("FIRE") == plan.exposure_count
        assert not peer.comp
    finally:
        driver.disconnect()


def test_live_lead_outside_soft_range_rejected_before_first_dark_move(rig):
    calibrations = dict(rig.c.axis_calibrations)
    calibrations[5] = replace(calibrations[5], max_coordinate=.025)
    rig.c.set_axis_calibrations(calibrations)
    rig.c.set_cutting_qualified(True)
    rig.c.configure_cutting_gate(rig.q.output, qualified=True)
    rig.q = replace(rig.q, axis_configuration=axis_configuration(rig.c))
    rig.snapshot = replace(rig.snapshot, motion_revision=rig.c.motion_revision,
                           axis_configuration=axis_configuration(rig.c))
    with pytest.raises(MotionControllerError, match="soft"):
        run(rig, plan_for(rig), live=True)
    assert not launched(rig.api) and "arm" not in rig.laser.calls


@pytest.mark.parametrize("change", ["hard_direction", "partner", "unknown_status"])
def test_preflight_uses_fresh_hard_status_and_x2_partner(rig, change):
    from automatic_transfer_station.gas_api import AXIS_STATUS_NEG_HARD_LIMIT
    original = rig.c.validate_cutting_path
    def changed(owner, endpoints):
        if change == "hard_direction":
            rig.api.statuses[5] = AXIS_STATUS_NEG_HARD_LIMIT
        elif change == "partner":
            rig.api.positions[7] = -1600000.
        else:
            rig.api.statuses[6] = 0x80000000
        return original(owner, endpoints)
    rig.c.validate_cutting_path = changed
    with pytest.raises((MotionControllerError, CuttingError)):
        run(rig, plan_for(rig), live=True)
    assert not launched(rig.api) and "arm" not in rig.laser.calls


def test_abort_wins_stop_race_without_false_cleanup_failure(rig):
    original = rig.c.stop_cutting
    def race(owner):
        original(owner)
        raise MotionControllerError("owner stale because independent abort won")
    rig.c.stop_cutting = race
    runner = RasterRunner(rig.c, rig.laser)
    def cancelled(*_):
        runner.cancel.set()
    with pytest.raises(CuttingError, match="cancelled"):
        run(rig, plan_for(rig), runner=runner, progress=cancelled)
    assert not runner.cleanup_required


def test_live_dot_unexpected_motion_during_settle_is_never_exposed(rig):
    from automatic_transfer_station.gas_controller import AXIS_STATUS_RUNNING
    plan = plan_for(rig, "dots", settle_ms=50)
    point = next(a.point_um for a in plan.actions if isinstance(a, DotExposure))
    target = tuple(rig.c.axis_calibrations[a].pulse_from_coordinate(v / 1000) for a, v in zip((5, 6), point))
    original = rig.laser.poll
    def drift():
        status = original()
        if not rig.api.running and tuple(rig.api.positions[a] for a in (5, 6)) == target:
            rig.api.statuses[5] = AXIS_STATUS_RUNNING
        return status
    rig.laser.poll = drift
    with pytest.raises(CuttingError, match="Stage moved"):
        run(rig, plan, live=True)
    assert "fire" not in rig.laser.calls


def test_completion_exception_reads_current_status_before_advancing_generation(rig):
    original = rig.laser.poll
    def concurrent_completion():
        was_firing = rig.laser.status.state is LaserState.FIRING
        result = original()
        if was_firing:
            raise CuttingError("pending STATUS cancelled by natural FIRE_DONE")
        return result
    rig.laser.poll = concurrent_completion
    plan = plan_for(rig, "dots")
    run(rig, plan, live=True)
    assert rig.laser.calls.count("fire") == plan.exposure_count


def test_wall_deadline_allows_serial_overhead_beyond_nominal_time(rig, monkeypatch):
    import automatic_transfer_station.cutting_raster_runner as module
    clock = SimpleNamespace(value=0.)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    runner = RasterRunner(rig.c, rig.laser)
    runner.cancel = ClockEvent(clock)
    plan = plan_for(rig, "dots")
    assert plan.estimated_duration_s < 1
    def communication_time(*_):
        clock.value += 2.
    run(rig, plan, runner=runner, progress=communication_time)
    assert clock.value > plan.estimated_duration_s * 3 + 5


def test_cleanup_does_not_steal_or_stop_a_new_owner_after_explicit_global_stop(rig):
    runner = RasterRunner(rig.c, rig.laser)
    rig.laser.fail_off = True
    with pytest.raises(CuttingError):
        run(rig, plan_for(rig), runner=runner)
    recovery = runner._recovery_owner
    assert recovery is not None
    rig.c.stop_all()
    owner = object()
    rig.c.acquire_cutting(owner)
    rig.laser.fail_off = False
    before = len([c for c in rig.api.calls if c[0] == "stop"])
    serial_before = len(rig.laser.calls)
    with pytest.raises(CuttingError, match="stale recovery"):
        runner.retry_cleanup()
    assert rig.c.get_cutting_lease_status(owner).valid
    assert len([c for c in rig.api.calls if c[0] == "stop"]) == before
    assert len(rig.laser.calls) == serial_before
    rig.c.release_cutting(owner)


@pytest.mark.parametrize('live', [False, True])
@pytest.mark.parametrize('boundary', ['polygon', 'roi_lower', 'roi_upper', 'thin_dot', 'concave'])
def test_quantized_exposure_must_stay_inside_roi_and_entire_polygon(rig, live, boundary):
    from automatic_transfer_station.cutting_raster import _inside, _segment_inside

    polygon = ((30.005, 30.005), (40.005, 30.005), (40.005, 40.005), (30.005, 40.005))
    roi = ROI(0, 0, 100, 100)
    settings = RasterSettings(row_pitch_um=5, speed_um_s=100)
    if boundary == 'roi_lower':
        roi = ROI(30.005, 30.005, 10, 10)
    elif boundary == 'roi_upper':
        polygon = ((20, 20), (50, 20), (50, 50), (20, 50))
        roi = ROI(30, 30, 10, 10)
    elif boundary == 'thin_dot':
        polygon = ((49.848, 49.848), (49.852, 49.848), (49.852, 49.852), (49.848, 49.852))
        settings = RasterSettings(mode='dots', row_pitch_um=.15, point_pitch_um=.15,
                                  speed_um_s=100, dwell_ms=1)
    elif boundary == 'concave':
        polygon = ((30, 30), (40, 30), (40, 40), (35.01, 40),
                   (35.01, 35.017), (34.99, 35.017), (34.99, 40), (30, 40))
        settings = RasterSettings(row_pitch_um=14.985, speed_um_s=100)
    plan = plan_raster(polygon, rig.snapshot.stage_um, rig.cal, roi,
                       BacklashSettings(2, 3), settings)
    # A geometric preview without controller calibration is valid; this is the
    # physical rounding step's responsibility, not a relaxation of the polygon.
    base_route = build_raster_route(plan)
    if boundary == 'concave':
        line = plan.lines[0]
        actual = []
        for point in (line.start_um, line.end_um):
            pose = tuple(rig.c.axis_calibrations[a].coordinate_from_pulse(
                rig.c.axis_calibrations[a].pulse_from_coordinate(v / 1000)) * 1000
                for a, v in zip((5, 6), point))
            actual.append(rig.cal.pixel_for_stage(pose, rig.snapshot.stage_um))
        assert all(roi.contains(p) and _inside(p, polygon) for p in actual)
        assert not _segment_inside(*actual, polygon)  # Endpoints alone are insufficient.
    with pytest.raises(CuttingError, match='Quantized.*(ROI|polygon)'):
        build_raster_route(plan, rig.q if live else None, rig.c, 500 if live else None)
    with pytest.raises(CuttingError, match='Quantized.*(ROI|polygon)'):
        # Even a previously accepted preview without pulse metadata must be
        # checked against the current board calibration at runtime.
        run(rig, plan, live=live, **({} if live else {'route': base_route}))
    assert not launched(rig.api)
    assert not any(command in rig.laser.calls for command in ('arm', 'fire', 'follow', 'keep'))
