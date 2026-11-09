"""Snapshot provenance and bounded, dark-only execution for the cutting workspace.

Camera frames are shared with CameraPanel; this module never opens a second camera.
All coordinates sent to the cutting motion API are X3/Y3 physical micrometres.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import math
import threading
import time
from typing import Callable

from .axis_calibration import AXIS_CALIBRATIONS
from .camera_controller import CameraFrame
from .cutting_geometry import CalibrationContext, CuttingPlan


class CuttingError(RuntimeError):
    pass


def axis_configuration(controller) -> str:
    calibration = getattr(controller, "axis_calibrations", AXIS_CALIBRATIONS)
    return json.dumps({str(k): asdict(v) for k, v in calibration.items()}, sort_keys=True, allow_nan=False)


def reference_positions(controller) -> tuple[tuple[int, float], ...]:
    values = controller.get_cutting_reference()
    if set(values) != set(range(1, 10)):
        raise CuttingError("Incomplete stage reference")
    result = tuple((axis, float(values[axis])) for axis in range(1, 10))
    if not all(math.isfinite(value) for _, value in result):
        raise CuttingError("Non-finite stage reference")
    return result


def same_positions(a, b, tolerance=0.5) -> bool:
    return len(a) == len(b) and all(
        axis_a == axis_b and abs(value_a - value_b) <= tolerance
        for (axis_a, value_a), (axis_b, value_b) in zip(a, b)
    )


@dataclass(frozen=True)
class FrameEnvelope:
    frame: CameraFrame
    context: CalibrationContext
    captured_at: float
    source_revision: int = 0


class SharedCuttingFrames:
    """Small thread-safe mailbox, fed by the existing camera's GUI signal."""

    def __init__(self):
        self._condition = threading.Condition()
        self.latest: FrameEnvelope | None = None
        self.closed = False

    def publish(self, frame: CameraFrame, objective: str, serial: str, source_revision: int = 0) -> None:
        if type(frame.binning) is not int or frame.binning < 1:
            return
        started = frame.metadata.get("capture_started_monotonic")
        if not isinstance(started, (float, int)) or not math.isfinite(started):
            return
        if not serial or not objective:
            return
        context = CalibrationContext(str(serial), str(objective), frame.width, frame.height, frame.binning)
        envelope = FrameEnvelope(replace(frame, metadata=dict(frame.metadata)), context, float(started), source_revision)
        with self._condition:
            if self.closed:
                return
            self.latest = envelope
            self._condition.notify_all()

    def wait_after(self, requested_at: float, cancel: threading.Event, timeout: float = 3.0) -> FrameEnvelope:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if cancel.is_set() or self.closed:
                    raise CuttingError("Capture cancelled")
                if self.latest is not None and self.latest.captured_at >= requested_at:
                    return self.latest
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CuttingError("No fresh camera frame; start preview and check the camera")
                self._condition.wait(min(remaining, 0.05))

    def close(self) -> None:
        with self._condition:
            self.closed = True
            self._condition.notify_all()


@dataclass(frozen=True)
class CuttingSnapshot:
    envelope: FrameEnvelope
    positions: tuple[tuple[int, float], ...]
    stage_um: tuple[float, float]
    motion_revision: int
    axis_configuration: str

    @property
    def context(self) -> CalibrationContext:
        return self.envelope.context

    def validate(self, controller, context: CalibrationContext) -> None:
        if context != self.context:
            raise CuttingError("Camera mode or objective changed; capture a new reference")
        if self.motion_revision != controller.motion_revision:
            raise CuttingError("Stage moved or was reset; capture a new reference")
        if self.axis_configuration != axis_configuration(controller):
            raise CuttingError("Axis calibration changed; capture a new reference")
        if not same_positions(self.positions, reference_positions(controller)):
            raise CuttingError("Stage reference changed; capture a new reference")


def capture_snapshot(controller, frames: SharedCuttingFrames, cancel: threading.Event,
                     fresh_frame: Callable[[], FrameEnvelope] | None = None) -> CuttingSnapshot:
    owner = object()
    controller.acquire_cutting(owner)
    try:
        before = reference_positions(controller)
        revision = controller.motion_revision
        configuration = axis_configuration(controller)
        requested_at = time.monotonic()
        envelope = fresh_frame() if fresh_frame is not None else frames.wait_after(requested_at, cancel)
        if (not math.isfinite(envelope.captured_at)
                or not requested_at <= envelope.captured_at <= time.monotonic()):
            raise CuttingError("Reference frame is not fresh")
        after = reference_positions(controller)
        if cancel.is_set() or revision != controller.motion_revision or not same_positions(before, after):
            raise CuttingError("Stage moved during capture")
        if axis_configuration(controller) != configuration:
            raise CuttingError("Axis calibration changed during capture")
        pose = tuple(float(value) for value in controller.get_cutting_pose())
        return CuttingSnapshot(envelope, after, pose, revision, configuration)
    finally:
        controller.release_cutting(owner)


class DarkCuttingRunner:
    """A dry run has no gate dependency and can never send an emission command.

The operator must physically inhibit the laser before running motion. This class
does not claim that a disconnected/unmonitored Comp signal confirms optical OFF.
"""

    def __init__(self, controller):
        self.controller = controller
        self.owner = object()
        self.cancel = threading.Event()
        self._active = False
        self._abort_lock = threading.Lock()
        self._abort_started = False

    def abort(self) -> None:
        self.cancel.set()
        with self._abort_lock:
            if self._abort_started:
                return
            self._abort_started = True
        # Emergency stop must not wait for the GUI worker or a camera frame.
        if self._active:
            threading.Thread(target=self._best_effort_stop, name="ATSCuttingStop", daemon=True).start()

    def _best_effort_stop(self) -> None:
        try:
            self.controller.stop_cutting(self.owner)
        except Exception:
            pass  # The worker reports unconfirmed stop/release as a fault.

    def run(self, plan: CuttingPlan, snapshot: CuttingSnapshot,
            context: CalibrationContext, progress: Callable[[int, int], None] = lambda *_: None,
            *, calibration=None, reference_guard: Callable[[], None] = lambda: None,
            path_endpoints=None) -> None:
        controller = self.controller
        controller.acquire_cutting(self.owner)
        self._active = True
        try:
            snapshot.validate(controller, context)
            plan.validate_for(calibration if calibration is not None else plan.calibration,
                              snapshot.context, snapshot.stage_um)
            reference_guard()
            targets = list(plan.path_endpoints_um if path_endpoints is None else path_endpoints)
            if (not targets or tuple(targets[0]) != snapshot.stage_um
                    or any(len(p) != 2 or any(not math.isfinite(v) for v in p) for p in targets)):
                raise CuttingError("Invalid dark rehearsal path")
            controller.validate_cutting_path(self.owner, targets)
            targets = [p for a, p in zip(targets, targets[1:]) if tuple(a) != tuple(p)]
            for index, target in enumerate(targets):
                reference_guard()
                if self.cancel.is_set():
                    raise CuttingError("Cutting cancelled")
                pose = controller.get_cutting_pose()
                duration = math.dist(pose, target) / plan.speed_um_s
                if not math.isfinite(duration) or duration > 3600:
                    raise CuttingError("Move duration exceeds one hour")
                controller.start_cutting_move(self.owner, target, plan.speed_um_s)
                deadline = time.monotonic() + max(3.0, duration * 3.0 + 2.0)
                while True:
                    reference_guard()
                    if self.cancel.is_set():
                        raise CuttingError("Cutting cancelled")
                    if controller.cutting_move_status(self.owner):
                        break
                    if time.monotonic() >= deadline:
                        raise CuttingError("Motion completion timed out")
                    self.cancel.wait(0.03)
                progress(index + 1, len(targets))
            controller.release_cutting(self.owner)
        except BaseException as fault:
            status_reader = getattr(controller, "get_cutting_lease_status", None)
            status = status_reader(self.owner) if callable(status_reader) else None
            already_stopped = status is not None and status.stop_confirmed and not status.cleanup_required
            try:
                if not already_stopped:
                    controller.stop_cutting(self.owner)
            except Exception as stop_fault:
                # abort() can win the lock after the first status read. A stale
                # owner error is harmless only if that exact lease is confirmed
                # stopped; never infer confirmation from an exception alone.
                latest = status_reader(self.owner) if callable(status_reader) else None
                if latest is None or latest.cleanup_required or not latest.stop_confirmed:
                    raise CuttingError(f"{fault}; stop unconfirmed: {stop_fault}") from fault
            if callable(getattr(controller, "get_cutting_lease_status", None)):
                status = controller.get_cutting_lease_status(self.owner)
                if status.cleanup_required or not status.stop_confirmed:
                    raise CuttingError(f"{fault}; stop unconfirmed; motion remains interlocked") from fault
            raise
        finally:
            self._active = False
