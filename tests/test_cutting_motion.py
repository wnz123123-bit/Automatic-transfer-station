"""DARK sample5/6 foundation tests. No DLL loading or hardware connection."""
import ctypes
from dataclasses import FrozenInstanceError, replace
import threading

import pytest

from automatic_transfer_station.axis_calibration import AXIS_CALIBRATIONS
from automatic_transfer_station.controller_base import MotionControllerError
from automatic_transfer_station.gas_api import GasApi, TCrdPrm
from automatic_transfer_station.gas_controller import (
    AXIS_STATUS_RUNNING, CUTTING_AXIS_MASK, GasMotionController,
)
from test_gas_api import FakeDll, FakeFunction
from test_gas_controller import FakeApi


class CuttingApi(FakeApi):
    supports_coordinated_motion = True

    def __init__(self):
        super().__init__()
        self.positions.update({5: 687898.0, 6: -250000.0, 7: -1133407.0})
        self.running = 0
        self.segment = 0
        self.queued = None
        self.failures = {}
        self.stop_confirms = True

    def record(self, name, *args):
        self.calls.append((name, *args))
        failure = self.failures.get(name, 0)
        if isinstance(failure, Exception):
            raise failure
        return failure

    def set_crd_prm(self, crd, prm):
        self.prm = TCrdPrm.from_buffer_copy(prm)
        return self.record("set_crd_prm", crd)

    def line_xy(self, crd, x, y, velocity, acceleration, end_velocity, fifo, segment):
        self.queued = (x, y)
        return self.record("line_xy", crd, x, y, velocity, acceleration, end_velocity, fifo, segment)

    def crd_start(self, mask, option):
        self.running = 1
        self.segment = 1
        self.statuses.update({5: AXIS_STATUS_RUNNING, 6: AXIS_STATUS_RUNNING})
        return self.record("crd_start", mask, option)

    def crd_status(self, crd, fifo):
        self.record("crd_status", crd, fifo)
        return self.running, self.segment

    def crd_clear(self, crd, fifo):
        result = self.record("crd_clear", crd, fifo)
        if not result:
            self.queued = None
        return result

    def stop(self, mask, option=0):
        result = self.record("stop", mask, option)
        if not result and self.stop_confirms:
            self.running = 0
            for axis in (5, 6):
                self.statuses[axis] = self.statuses.get(axis, 0) & ~AXIS_STATUS_RUNNING
        return result

    def finish(self):
        self.positions[5], self.positions[6] = self.queued
        self.running = 0
        self.statuses[5] = self.statuses[6] = 0


@pytest.fixture
def rig():
    api = CuttingApi()
    controller = GasMotionController(api, cutting_qualified=True)
    controller.is_open = True  # fake only; never call real GA_Open
    owner = object()
    controller.acquire_cutting(owner)
    api.calls.clear()
    return controller, api, owner


def writes(api):
    return [call for call in api.calls if call[0] not in (
        "get_status", "get_prf_pos", "crd_status")]


def test_packed_vendor_coordinate_layout_and_fixed_windows_long():
    assert ctypes.sizeof(TCrdPrm) == 70
    assert ctypes.alignment(TCrdPrm) == 2
    assert {name: getattr(TCrdPrm, name).offset for name, *_ in TCrdPrm._fields_} == {
        "dimension": 0, "profile": 2, "synVelMax": 18, "synAccMax": 26,
        "evenTime": 34, "setOriginFlag": 36, "originPos": 38,
    }
    assert ctypes.sizeof(TCrdPrm._fields_[-1][1]) == 32


def coordinate_dll():
    dll = FakeDll()
    for name in ("GA_SetCrdPrm", "GA_LnXY", "GA_CrdStart", "GA_CrdStatus", "GA_CrdClear"):
        setattr(dll, name, FakeFunction())
    return dll


def test_optional_exports_leave_legacy_dll_usable_and_report_capability():
    dll = FakeDll()
    api = GasApi(dll)
    assert api.supports_coordinated_motion is False
    assert api.stop(48) == 0
    with pytest.raises(RuntimeError, match="unavailable"):
        api.crd_start(1)
    dll = coordinate_dll()
    del dll.GA_CrdClear
    assert GasApi(dll).supports_coordinated_motion is False


def test_coordinate_signatures_and_output_marshalling():
    dll = coordinate_dll()
    api = GasApi(dll)
    assert api.supports_coordinated_motion
    assert dll.GA_SetCrdPrm.argtypes == [ctypes.c_int16, ctypes.POINTER(TCrdPrm)]
    assert dll.GA_LnXY.argtypes == [ctypes.c_int16, ctypes.c_int32, ctypes.c_int32,
        ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_int16, ctypes.c_int32]
    assert dll.GA_CrdStart.argtypes == [ctypes.c_int16, ctypes.c_int16]
    assert dll.GA_CrdStatus.argtypes == [ctypes.c_int16, ctypes.POINTER(ctypes.c_int16),
                                        ctypes.POINTER(ctypes.c_int32), ctypes.c_int16]
    assert dll.GA_CrdClear.argtypes == [ctypes.c_int16, ctypes.c_int16]
    prm = TCrdPrm()
    assert api.set_crd_prm(1, prm) == 0
    assert dll.GA_SetCrdPrm.calls[0][1]._obj is prm
    assert api.line_xy(1, -10, 20, 0.05, 0.1, 0, 0, 7) == 0
    assert dll.GA_LnXY.calls == [(1, -10, 20, 0.05, 0.1, 0, 0, 7)]
    assert api.crd_start(1, 0) == api.crd_clear(1, 0) == 0

    def fill(crd, run, segment, fifo):
        run._obj.value, segment._obj.value = 1, 17
        return 0
    dll.GA_CrdStatus.side_effect = fill
    assert api.crd_status(1) == (1, 17)
    dll.GA_CrdStatus.side_effect = lambda *_: -7
    with pytest.raises(RuntimeError, match="-7"):
        api.crd_status(1)
    with pytest.raises(ValueError, match="LONG"):
        api.line_xy(1, 2**31, 0, 1, 1)


@pytest.mark.parametrize("speed,velocity", [(1.0, 0.05), (1000, 50.0)])
def test_fixed_sample_axis_map_negative_sign_absolute_origin_and_vector_speed(rig, speed, velocity):
    c, api, owner = rig
    c.start_cutting_move(owner, (100, 200), speed)
    assert api.prm.dimension == 2
    assert list(api.prm.profile) == [5, 6, 0, 0, 0, 0, 0, 0]
    assert list(api.prm.originPos) == [0] * 8
    assert api.prm.setOriginFlag == 1
    assert api.prm.synVelMax == pytest.approx(velocity)
    assert writes(api) == [("crd_clear", 1, 0), ("set_crd_prm", 1),
        ("line_xy", 1, 682898, -260000, velocity, 0.1, 0.0, 0, 1), ("crd_start", 1, 0)]
    assert c.cutting_move_status(owner) is False
    api.finish()
    assert c.cutting_move_status(owner) is True
    assert c.get_cutting_pose() == pytest.approx((100, 200))
    c.release_cutting(owner)
    assert not c.get_cutting_lease_status(owner).valid


def test_capture_lease_without_qualification_or_coordinate_exports():
    api = FakeApi()
    api.positions.update({5: 687898, 6: -250000, 7: -1133407})
    c = GasMotionController(api)
    c.is_open = True
    owner = object()
    c.acquire_cutting(owner)
    revision = c.motion_revision
    assert not c.cutting_qualified
    assert c.get_cutting_pose() == (0.0, 0.0)
    assert set(c.get_cutting_reference()) == set(range(1, 10))
    c.validate_cutting_path(owner, [(0, 0), (10, 20)])
    with pytest.raises(MotionControllerError, match="board-qualified"):
        c.start_cutting_move(owner, (10, 20), 1)
    c.release_cutting(owner)
    assert c.motion_revision == revision
    assert writes(api) == []


def test_explicit_qualification_no_fake_auto_bypass_and_revoked_on_config():
    api = CuttingApi()
    c = GasMotionController(api)
    c.is_open = True
    assert not c.cutting_qualified
    c.set_cutting_qualified(True)
    assert c.cutting_qualified
    calibration = dict(c.axis_calibrations)
    c.axis_calibrations = calibration
    assert not c.cutting_qualified
    calibration[5] = replace(calibration[5], zero_pulse=0)
    assert c.axis_calibrations[5].zero_pulse != 0
    with pytest.raises(TypeError):
        c.axis_calibrations[5] = calibration[5]
    with pytest.raises(MotionControllerError, match="explicit bool"):
        c.set_cutting_qualified("True")


@pytest.mark.parametrize("axis", range(1, 10))
@pytest.mark.parametrize("status", [0x400, 0x1000])
def test_acquire_rejects_inflight_all_axes_and_home(axis, status):
    api = CuttingApi()
    api.statuses[axis] = status
    c = GasMotionController(api)
    c.is_open = True
    with pytest.raises(MotionControllerError):
        c.acquire_cutting(object())
    assert writes(api) == []


def test_acquire_rejects_closed_and_active_home_owner():
    c = GasMotionController(CuttingApi())
    with pytest.raises(MotionControllerError, match="not open"):
        c.acquire_cutting(object())
    c.is_open = True
    c._home_owner = object()
    with pytest.raises(MotionControllerError, match="homing"):
        c.acquire_cutting(object())


@pytest.mark.parametrize("axis,launch", [
    (3, lambda c: c.move_axis3_to_position(100)),
    (6, lambda c: c.move_axis_to_coordinate(6, 0.1)),
    (9, lambda c: c.move_axis9_to_angle(1)),
])
def test_acquire_rejects_accepted_manual_move_before_running_bit_updates(axis, launch):
    api = CuttingApi()
    c = GasMotionController(api)
    c.is_open = True
    old_position = api.positions.get(axis, 0)
    launch(c)
    target = api.positions[axis]
    api.positions[axis] = old_position  # delayed planner/running telemetry
    with pytest.raises(MotionControllerError, match="manual target"):
        c.acquire_cutting(object())
    api.positions[axis] = target
    c.acquire_cutting(object())


@pytest.mark.parametrize("call", [
    lambda c: c.relative_move(1, 1, 1, 1),
    lambda c: c.relative_move(5, 1, 1, 1),
    lambda c: c.move_axis_to_coordinate(7, 0, 1),
    lambda c: c.start_jog(3, 1),
    lambda c: c.start_jog(6, 0),
    lambda c: c.move_axis3_to_position(0),
    lambda c: c.move_axis3_to_mark(),
    lambda c: c.move_axis9_to_angle(0),
    lambda c: c.home_axis9(),
    lambda c: c.new_axis9_home_job(),
    lambda c: c.zero_axis3(),
    lambda c: c.mark_axis3(),
    lambda c: c.reset_board(),
    lambda c: c.open_board(),
    lambda c: c.set_axis_calibrations(AXIS_CALIBRATIONS),
    lambda c: setattr(c, "axis_calibrations", AXIS_CALIBRATIONS),
    lambda c: c.set_cutting_qualified(True),
])
def test_all_manual_gamepad_and_configuration_paths_rejected_during_lease(rig, call):
    c, api, owner = rig
    with pytest.raises(MotionControllerError):
        call(c)
    assert writes(api) == []
    assert c.get_cutting_lease_status(owner).valid


@pytest.mark.parametrize("target", [(float("nan"), 0), (0, float("inf")),
    (13000, 0), (0, -14000), (0, 0, 9), None])
def test_targets_finite_shape_and_soft_bounds_before_any_fifo_mutation(rig, target):
    c, api, owner = rig
    with pytest.raises(MotionControllerError):
        c.start_cutting_move(owner, target, 1)
    assert writes(api) == []


@pytest.mark.parametrize("speed", [0, -1, float("nan"), float("inf"), 5001, "bad"])
def test_speed_is_finite_positive_and_bounded(rig, speed):
    c, api, owner = rig
    with pytest.raises(MotionControllerError):
        c.start_cutting_move(owner, (10, 20), speed)
    assert writes(api) == []


@pytest.mark.parametrize("field,value", [("unit", "um"), ("pulses_per_unit", -1),
    ("pulses_per_unit", float("nan")), ("zero_pulse", float("inf")),
    ("pulse_sign", 0), ("min_coordinate", None), ("pulses_per_unit", 40000)])
def test_invalid_unit_scale_calibration_rejected(field, value):
    api = CuttingApi()
    c = GasMotionController(api)
    c.is_open = True
    cal = dict(AXIS_CALIBRATIONS)
    cal[5] = replace(cal[5], **{field: value})
    c.set_axis_calibrations(cal)
    with pytest.raises(MotionControllerError):
        c.acquire_cutting(object())
    assert writes(api) == []


def test_full_path_preload_and_later_endpoints_checked_before_any_move(rig):
    c, api, owner = rig
    for path in [[(20000, 0), (10, 20)], [(10, 20), (0, 14000)],
                 [(10, 20), (12500, 0)], []]:
        with pytest.raises(MotionControllerError):
            c.validate_cutting_path(owner, path)
    # 12500um is inside X3 soft travel but violates X2/X3 separation.
    assert writes(api) == []


def test_start_rechecks_changed_partner_and_startpoint(rig):
    c, api, owner = rig
    c.validate_cutting_path(owner, [(10, 20)])
    api.positions[7] = -2_000_000
    with pytest.raises(MotionControllerError, match="interference"):
        c.start_cutting_move(owner, (10, 20), 1)
    assert writes(api) == []


def test_preflight_reads_fresh_partner_and_hard_status_for_each_later_endpoint(rig):
    c, api, owner = rig

    def path():
        yield (10, 20)
        api.statuses[6] = 0x40
        yield (20, 30)

    with pytest.raises(MotionControllerError, match="abnormal"):
        c.validate_cutting_path(owner, path())
    assert writes(api) == []


def test_long_preflight_releases_lock_between_endpoints_for_cancellation(rig):
    c, api, owner = rig
    between, proceed, stopped = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def path():
        yield (10, 20)
        between.set()
        assert proceed.wait(2)
        yield (20, 30)

    def validate():
        try:
            c.validate_cutting_path(owner, path())
        except MotionControllerError as exc:
            errors.append(exc)

    def stop():
        c.stop_cutting(owner)
        stopped.set()

    worker = threading.Thread(target=validate)
    worker.start()
    assert between.wait(2)
    stopper = threading.Thread(target=stop)
    stopper.start()
    try:
        assert stopped.wait(1)
    finally:
        proceed.set()
        worker.join(2)
        stopper.join(2)
    assert len(errors) == 1 and "stale" in str(errors[0])
    assert not any(call[0] == "line_xy" for call in api.calls)


def test_target_int32_overflow_rejected_without_wrapping():
    api = CuttingApi()
    c = GasMotionController(api)
    c.is_open = True
    cal = dict(AXIS_CALIBRATIONS)
    cal[5] = replace(cal[5], zero_pulse=2**31 - 1)
    c.set_axis_calibrations(cal)
    c.set_cutting_qualified(True)
    api.positions[5] = cal[5].zero_pulse
    owner = object()
    c.acquire_cutting(owner)
    with pytest.raises(MotionControllerError, match="int32"):
        c.start_cutting_move(owner, (-1, 0), 1)
    assert writes(api) == []


@pytest.mark.parametrize("axis", [5, 6, 7])
def test_nonfinite_planner_and_partner_positions_fail_closed(rig, axis):
    c, api, owner = rig
    api.positions[axis] = float("nan")
    with pytest.raises(MotionControllerError, match="finite"):
        c.validate_cutting_path(owner, [(10, 20)])
    assert writes(api) == []


@pytest.mark.parametrize("bit", [0x2, 0x4, 0x8, 0x10, 0x20, 0x40, 0x80, 0x100, 0x80000000])
def test_abnormal_axis_status_before_move_is_refused(rig, bit):
    c, api, owner = rig
    api.statuses[5] = bit
    with pytest.raises(MotionControllerError):
        c.start_cutting_move(owner, (100, 20), 1)
    assert writes(api) == []


@pytest.mark.parametrize("method", ["crd_clear", "set_crd_prm", "line_xy", "crd_start"])
@pytest.mark.parametrize("failure", [-7, RuntimeError("transport lost")])
def test_partial_launch_failure_stops_both_axes_attempts_fifo_cleanup_invalidates_owner(rig, method, failure):
    c, api, owner = rig
    api.failures[method] = failure
    with pytest.raises(MotionControllerError):
        c.start_cutting_move(owner, (10, 20), 1)
    assert ("stop", CUTTING_AXIS_MASK, 0) in api.calls
    assert api.calls.count(("crd_clear", 1, 0)) == 2
    assert not c.get_cutting_lease_status(owner).valid
    with pytest.raises(MotionControllerError, match="stale"):
        c.start_cutting_move(owner, (10, 20), 1)


def test_failed_cleanup_keeps_manual_interlock_until_explicit_retry(rig):
    c, api, owner = rig
    c.start_cutting_move(owner, (10, 20), 1)
    api.failures["crd_clear"] = -7
    with pytest.raises(MotionControllerError, match="CrdClear"):
        c.stop_cutting(owner)
    with pytest.raises(MotionControllerError, match="recovery"):
        c.start_jog(3, 1)
    with pytest.raises(MotionControllerError, match="recovery"):
        c.acquire_cutting(object())
    api.failures.clear()
    c.stop_all()
    assert ("stop", 0xFFFF, 0) in api.calls
    c.acquire_cutting(object())


def test_stop_attempts_fifo_clear_even_when_stop_fails(rig):
    c, api, owner = rig
    c.start_cutting_move(owner, (10, 20), 1)
    api.failures["stop"] = -7
    api.calls.clear()
    with pytest.raises(MotionControllerError):
        c.stop_cutting(owner)
    assert writes(api) == [("stop", 48, 0), ("crd_clear", 1, 0)]
    assert not c.get_cutting_lease_status(owner).valid


def test_decelerating_stop_is_not_confirmation(rig):
    c, api, owner = rig
    c.start_cutting_move(owner, (10, 20), 1)
    api.stop_confirms = False
    with pytest.raises(MotionControllerError, match="not yet confirmed"):
        c.stop_cutting(owner)
    status = c.get_cutting_lease_status(owner)
    assert status.state == "fault"
    assert status.can_retry_stop and status.cleanup_required
    assert not status.valid and not status.stop_confirmed
    api.stop_confirms = True
    c.stop_cutting(owner)
    status = c.get_cutting_lease_status(owner)
    assert status.stop_confirmed and not status.cleanup_required and not status.can_retry_stop
    c.acquire_cutting(object())
    with pytest.raises(MotionControllerError, match="stale"):
        c.stop_cutting(owner)


@pytest.mark.parametrize("action,mask", [(lambda c, o: c.stop_cutting(o), 48),
    (lambda c, o: c.stop_all(), 0xFFFF), (lambda c, o: c.stop_axis(5), 48),
    (lambda c, o: c.stop_axis(7), 112), (lambda c, o: c.close_board(), 0xFFFF)])
def test_cancellation_stale_owner_and_combined_stop_mask(rig, action, mask):
    c, api, owner = rig
    c.start_cutting_move(owner, (10, 20), 1)
    action(c, owner)
    assert ("stop", mask, 0) in api.calls
    assert not c.get_cutting_lease_status(owner).valid
    for operation in [lambda: c.start_cutting_move(owner, (20, 30), 1),
                      lambda: c.cutting_move_status(owner), lambda: c.release_cutting(owner)]:
        with pytest.raises(MotionControllerError):
            operation()


def test_old_owner_cannot_stop_new_owner_or_reacquire(rig):
    c, api, old = rig
    c.stop_cutting(old)
    with pytest.raises(MotionControllerError, match="fresh"):
        c.acquire_cutting(old)
    new = object()
    c.acquire_cutting(new)
    api.calls.clear()
    with pytest.raises(MotionControllerError, match="stale"):
        c.stop_cutting(old)
    assert api.calls == []
    assert c.get_cutting_lease_status(new).valid


def test_completion_requires_axis_idle_and_target_pulses(rig):
    c, api, owner = rig
    c.start_cutting_move(owner, (10, 20), 1)
    api.running = 0
    assert not c.cutting_move_status(owner)  # CRD idle alone is insufficient
    api.statuses[5] = api.statuses[6] = 0
    with pytest.raises(MotionControllerError, match="target"):
        c.cutting_move_status(owner)
    assert not c.get_cutting_lease_status(owner).valid


def test_within_one_planner_pulse_tolerance_can_finish_and_release(rig):
    c, api, owner = rig
    c.start_cutting_move(owner, (10, 20), 1)
    api.finish()
    api.positions[5] += 0.75
    assert c.cutting_move_status(owner)
    c.release_cutting(owner)
    with pytest.raises(FrozenInstanceError):
        c.get_cutting_lease_status(owner).valid = True


def test_release_while_running_retains_owner(rig):
    c, api, owner = rig
    c.start_cutting_move(owner, (10, 20), 1)
    with pytest.raises(MotionControllerError, match="running"):
        c.release_cutting(owner)
    assert c.get_cutting_lease_status(owner).valid


@pytest.mark.parametrize("fault", ["crd_transport", "axis_alarm", "unknown_crd"])
def test_status_fault_cancels_and_cleans_fifo(rig, fault):
    c, api, owner = rig
    c.start_cutting_move(owner, (10, 20), 1)
    if fault == "crd_transport":
        api.failures["crd_status"] = RuntimeError("offline")
    elif fault == "axis_alarm":
        api.statuses[6] |= 0x2
    else:
        api.running = 7
    with pytest.raises(MotionControllerError):
        c.cutting_move_status(owner)
    assert ("stop", 48, 0) in api.calls
    assert not c.get_cutting_lease_status(owner).valid


def test_close_always_attempted_despite_stop_and_clear_exceptions(rig):
    c, api, owner = rig
    c.start_cutting_move(owner, (10, 20), 1)
    api.failures.update(stop=RuntimeError("stop failed"), crd_clear=RuntimeError("clear failed"))
    with pytest.raises(MotionControllerError):
        c.close_board()
    assert ("close",) in api.calls
    assert not c.is_open
    assert not c.cutting_qualified
    assert not c.get_cutting_lease_status(owner).valid


def test_reference_finite_all_axes_and_read_only_revision(rig):
    c, api, owner = rig
    revision = c.motion_revision
    reference = c.get_cutting_reference()
    assert set(reference) == set(range(1, 10))
    assert reference[5] == 687898
    c.get_cutting_pose()
    c.get_cutting_lease_status(owner)
    c.cutting_move_status(owner)
    assert c.motion_revision == revision
    api.positions[9] = float("inf")
    with pytest.raises(MotionControllerError, match="finite"):
        c.get_cutting_reference()


def test_revision_records_move_away_return_zero_reset_calibration_open_close_and_home():
    api = CuttingApi()
    c = GasMotionController(api)
    revision = c.motion_revision
    for operation in [c.open_board, lambda: c.move_axis3_to_position(100),
        lambda: c.move_axis3_to_position(0), c.zero_axis3,
        lambda: c.relative_move(3, 1, 1, 5), lambda: c.start_jog(3, 1),
        lambda: c.move_axis_to_coordinate(6, 0), lambda: c.move_axis9_to_angle(0),
        lambda: c.set_axis_calibrations(AXIS_CALIBRATIONS), c.reset_board]:
        operation()
        assert c.motion_revision > revision
        revision = c.motion_revision
    api.positions[9] = 0
    c.new_axis9_home_job().poll()
    assert c.motion_revision > revision
    revision = c.motion_revision
    c.close_board()
    assert c.motion_revision > revision


def test_queued_home_cannot_resume_after_capture_lease():
    c = GasMotionController(CuttingApi())
    c.is_open = True
    job = c.new_axis9_home_job()
    owner = object()
    c.acquire_cutting(owner)
    with pytest.raises(MotionControllerError):
        job.poll()
    c.release_cutting(owner)
    with pytest.raises(MotionControllerError):
        job.poll()


def test_launch_serialized_against_emergency_stop_and_reference_reads(rig):
    c, api, owner = rig
    entered, proceed, stop_done = threading.Event(), threading.Event(), threading.Event()
    original_line = api.line_xy
    errors = []

    def line(*args):
        entered.set()
        assert proceed.wait(2)
        return original_line(*args)

    def launch():
        try:
            c.start_cutting_move(owner, (10, 20), 1)
        except Exception as exc:
            errors.append(exc)

    def stop():
        try:
            c.stop_all()
        except Exception as exc:
            errors.append(exc)
        finally:
            stop_done.set()

    api.line_xy = line
    worker = threading.Thread(target=launch)
    worker.start()
    assert entered.wait(2)
    stopper = threading.Thread(target=stop)
    stopper.start()
    assert not stop_done.wait(0.03)
    proceed.set()
    worker.join(2)
    stopper.join(2)
    assert not worker.is_alive() and not stopper.is_alive()
    assert errors == []
    names = [call[0] for call in writes(api)]
    assert names.index("crd_start") < names.index("stop")
    assert not c.get_cutting_lease_status(owner).valid
