import threading
import time
from dataclasses import replace

import pytest

from automatic_transfer_station.camera_controller import CameraFrame
from automatic_transfer_station.cutting_geometry import (
    BacklashSettings, CalibrationContext, CalibrationSample, ROI,
    fit_calibration, plan_line,
)
from automatic_transfer_station.cutting_session import (
    CuttingError, CuttingSnapshot, DarkCuttingRunner, FrameEnvelope,
    SharedCuttingFrames, axis_configuration, capture_snapshot,
)


class Motion:
    def __init__(self):
        self.motion_revision = 0
        self.positions = {a: 0.0 for a in range(1, 10)}
        self.pose = (0., 0.)
        self.owner = None
        self.calls = []
        self.done = True
        self.invalid = False

    def acquire_cutting(self, owner):
        assert self.owner is None
        self.owner = owner
        self.calls.append('acquire')

    def release_cutting(self, owner):
        assert owner is self.owner
        self.owner = None
        self.calls.append('release')

    def get_cutting_reference(self):
        return dict(self.positions)

    def get_cutting_pose(self):
        return self.pose

    def validate_cutting_path(self, owner, targets):
        self.calls.append(('validate', tuple(targets)))
        if self.invalid:
            raise CuttingError('unsafe preload')

    def start_cutting_move(self, owner, target, speed):
        assert owner is self.owner
        self.pose = target
        self.motion_revision += 1
        self.calls.append(('move', target, speed))

    def cutting_move_status(self, owner):
        assert owner is self.owner
        return self.done

    def stop_cutting(self, owner):
        self.calls.append('stop')
        self.owner = None


@pytest.fixture
def reference():
    motion = Motion()
    context = CalibrationContext('camera', '20X', 100, 100, 1)
    frame = CameraFrame(100, 100, b'\0' * 30000)
    snapshot = CuttingSnapshot(FrameEnvelope(frame, context, time.monotonic()),
        tuple(motion.positions.items()), motion.pose, 0, axis_configuration(motion))
    cal = fit_calibration([
        CalibrationSample((0, 0), (30, 30)),
        CalibrationSample((10, 0), (40, 30)),
        CalibrationSample((0, 10), (30, 40)),
    ], (50, 50), context, 0.01)
    plan = plan_line((30, 30), (40, 45), motion.pose, cal, ROI(0, 0, 100, 100),
                     BacklashSettings(2, 3), 100)
    return motion, snapshot, plan


def test_fresh_reference_is_captured_under_motion_lease(reference):
    motion, snapshot, _ = reference
    def fresh():
        assert motion.owner is not None
        return replace(snapshot.envelope, captured_at=time.monotonic())
    result = capture_snapshot(motion, SharedCuttingFrames(), threading.Event(), fresh)
    assert result.positions == snapshot.positions
    assert result.stage_um == snapshot.stage_um
    assert motion.calls == ['acquire', 'release']


@pytest.mark.parametrize('change', ['axis', 'revision', 'cancel'])
def test_capture_invalidated_if_reference_changes(reference, change):
    motion, snapshot, _ = reference
    cancel = threading.Event()
    def fresh():
        if change == 'axis':
            motion.positions[9] = 1
        elif change == 'revision':
            motion.motion_revision += 1
        else:
            cancel.set()
        return replace(snapshot.envelope, captured_at=time.monotonic())
    with pytest.raises(CuttingError):
        capture_snapshot(motion, SharedCuttingFrames(), cancel, fresh)
    assert motion.owner is None


@pytest.mark.parametrize('change', ['axis', 'revision', 'camera', 'plan_pose'])
def test_stale_snapshot_or_plan_never_moves(reference, change):
    motion, snapshot, plan = reference
    context = snapshot.context
    if change == 'axis':
        motion.positions[3] = 2
    elif change == 'revision':
        motion.motion_revision += 2  # Moving away and returning is still stale.
    elif change == 'camera':
        context = replace(context, binning=2)
    else:
        snapshot = replace(snapshot, stage_um=(1., 0.))
    with pytest.raises((CuttingError, ValueError)):
        DarkCuttingRunner(motion).run(plan, snapshot, context)
    assert not any(isinstance(c, tuple) and c[0] == 'move' for c in motion.calls)


def test_entire_path_validated_before_any_dark_motion(reference):
    motion, snapshot, plan = reference
    motion.invalid = True
    with pytest.raises(CuttingError, match='unsafe preload'):
        DarkCuttingRunner(motion).run(plan, snapshot, snapshot.context)
    assert not any(isinstance(c, tuple) and c[0] == 'move' for c in motion.calls)


def test_dark_run_includes_preload_and_has_no_gate_interface(reference):
    motion, snapshot, plan = reference
    DarkCuttingRunner(motion).run(plan, snapshot, snapshot.context)
    assert motion.calls[1] == ('validate', plan.path_endpoints_um)
    moves = [c[1] for c in motion.calls if isinstance(c, tuple) and c[0] == 'move']
    assert moves == [s.end_um for s in plan.segments]
    assert motion.calls[-1] == 'release'


def test_cancel_stops_without_waiting_for_move_completion(reference):
    motion, snapshot, plan = reference
    motion.done = False
    runner = DarkCuttingRunner(motion)
    errors = []
    def run():
        try:
            runner.run(plan, snapshot, snapshot.context)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 1
    while not any(isinstance(c, tuple) and c[0] == 'move' for c in motion.calls):
        assert time.monotonic() < deadline
        time.sleep(.002)
    runner.abort()
    thread.join(1)
    assert not thread.is_alive()
    assert errors
    assert 'stop' in motion.calls


def test_camera_mailbox_rejects_old_unknown_and_cancelled_frames(reference):
    _, snapshot, _ = reference
    frames = SharedCuttingFrames()
    frames.publish(snapshot.envelope.frame, '20X', 'camera')
    assert frames.latest is None
    frames.publish(replace(snapshot.envelope.frame, metadata={'capture_started_monotonic': 1.}), '20X', '')
    assert frames.latest is None
    frames.publish(replace(snapshot.envelope.frame, metadata={'capture_started_monotonic': 1.}), '20X', 'camera')
    with pytest.raises(CuttingError, match='fresh'):
        frames.wait_after(2., threading.Event(), .005)
    frames.close()
    with pytest.raises(CuttingError, match='cancelled'):
        frames.wait_after(0., threading.Event())


def test_reference_grab_flushes_preview_and_restores_on_failure():
    from automatic_transfer_station.camera_controller import BaslerCameraController, CameraError
    from unittest.mock import Mock
    controller = BaslerCameraController(backend=Mock())
    handle = Mock()
    controller._handle = handle
    controller._previewing = True
    frame = CameraFrame(1, 1, b'\0\0\0')
    handle.grab_rgb.return_value = frame
    assert controller.grab_reference(10) is frame
    assert [c[0] for c in handle.mock_calls] == ['stop_preview', 'grab_rgb', 'start_preview']
    handle.reset_mock()
    handle.grab_rgb.side_effect = RuntimeError('timeout')
    with pytest.raises(CameraError, match='timeout'):
        controller.grab_reference(10)
    handle.start_preview.assert_called_once()
    assert controller._previewing


def test_stale_callback_frame_cannot_be_rebound_to_current_pose(reference):
    motion, snapshot, _ = reference
    with pytest.raises(CuttingError, match='not fresh'):
        capture_snapshot(motion, SharedCuttingFrames(), threading.Event(), lambda: snapshot.envelope)
    assert motion.owner is None


def test_changed_calibration_blocks_execution(reference):
    motion, snapshot, plan = reference
    with pytest.raises(ValueError, match='stale'):
        DarkCuttingRunner(motion).run(plan, snapshot, snapshot.context,
            calibration=replace(plan.calibration, revision='new-fit'))
    assert not any(isinstance(c, tuple) and c[0] == 'move' for c in motion.calls)


def test_mid_run_optical_change_stops_remaining_segments(reference):
    motion, snapshot, plan = reference
    def guard():
        if motion.motion_revision > 0:
            raise CuttingError('objective changed')
    with pytest.raises(CuttingError, match='objective'):
        DarkCuttingRunner(motion).run(plan, snapshot, snapshot.context, reference_guard=guard)
    assert len([c for c in motion.calls if isinstance(c, tuple) and c[0] == 'move']) == 1
    assert 'stop' in motion.calls


def test_confirmed_global_stop_is_not_reported_unconfirmed(reference):
    from types import SimpleNamespace
    motion, snapshot, plan = reference
    runner = DarkCuttingRunner(motion)
    motion.get_cutting_lease_status = lambda owner: SimpleNamespace(
        stop_confirmed=True, cleanup_required=False)
    def stop_in_poll(owner):
        motion.owner = None
        runner.cancel.set()
        return False
    motion.cutting_move_status = stop_in_poll
    with pytest.raises(CuttingError, match='Cutting cancelled') as raised:
        runner.run(plan, snapshot, snapshot.context)
    assert 'unconfirmed' not in str(raised.value)
    assert 'stop' not in motion.calls


def test_parallel_abort_can_confirm_between_status_read_and_stop(reference):
    from types import SimpleNamespace
    motion, snapshot, plan = reference
    runner = DarkCuttingRunner(motion)
    motion.invalid = True
    confirmed = False
    motion.get_cutting_lease_status = lambda owner: SimpleNamespace(
        stop_confirmed=confirmed, cleanup_required=not confirmed)
    def parallel_stop_wins(owner):
        nonlocal confirmed
        confirmed = True
        raise RuntimeError('owner stale after parallel stop')
    motion.stop_cutting = parallel_stop_wins
    with pytest.raises(CuttingError, match='unsafe preload') as raised:
        runner.run(plan, snapshot, snapshot.context)
    assert 'unconfirmed' not in str(raised.value)


def test_reference_capture_preserves_both_grab_and_restore_errors():
    from automatic_transfer_station.camera_controller import BaslerCameraController, CameraError
    from unittest.mock import Mock
    controller = BaslerCameraController(backend=Mock())
    controller._handle = Mock()
    controller._previewing = True
    controller._handle.grab_rgb.side_effect = RuntimeError('capture timeout')
    controller._handle.start_preview.side_effect = RuntimeError('restore rejected')
    with pytest.raises(CameraError) as raised:
        controller.grab_reference()
    assert 'capture timeout' in str(raised.value) and 'restore rejected' in str(raised.value)
    assert controller.is_previewing is False


def test_repeated_abort_during_blocked_stop_starts_only_one_worker(reference):
    motion, _, _ = reference
    runner = DarkCuttingRunner(motion)
    runner._active = True
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def stop(owner):
        calls.append(owner)
        entered.set()
        release.wait(2)
        finished.set()
    motion.stop_cutting = stop
    try:
        for _ in range(5):
            runner.abort()
        assert entered.wait(.5)
        assert calls == [runner.owner]
    finally:
        release.set()
        assert finished.wait(.5)
