"""Immutable raster routes and leased execution. No device is opened here.

The builder uses calibration VALUES only; it is safe with a disconnected proxy.
Preview, dark rehearsal and live execution share the same spatial route. All
feedback is planner/electrical state, never an encoder or optical measurement.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
import threading
import time
from types import SimpleNamespace

from .axis_calibration import AxisCalibration
from .cutting_geometry import plan_line
from .cutting_live import LiveLinePath, LiveQualification, plan_live_line
from .cutting_raster import DotExposure, RasterMove, RasterPlan, _inside, _segment_inside
from .cutting_session import CuttingError, axis_configuration
from .gas_controller import CUTTING_MAX_SPEED_UM_S, CUTTING_POSITION_TOLERANCE_PULSE
from .gas_controller import CUTTING_ACCELERATION_PULSE_MS2


MAX_ACTIONS = 30000
MAX_EXPOSURES = 10000
MAX_JOB_SECONDS = 3600.0
_POLL_SECONDS = .02
_KEEP_SECONDS = .075


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise CuttingError(f"{name} must be finite")
    return float(value)


def _calibrations(controller):
    if controller is None:
        return ()
    values = tuple(sorted(controller.axis_calibrations.items()))
    by_axis = dict(values)
    for axis in (5, 6):
        c = by_axis.get(axis)
        if not isinstance(c, AxisCalibration) or c.axis != axis or c.unit != "mm":
            raise CuttingError("Raster requires calibrated X3/Y3 axes 5/6 in mm")
        if (_number(c.pulses_per_unit, "pulse/mm") <= 0 or isinstance(c.pulse_sign, bool)
                or c.pulse_sign not in (-1, 1)):
            raise CuttingError("Invalid X3/Y3 pulse scale/sign")
        for name in ("zero_pulse", "zero_coordinate", "min_coordinate", "max_coordinate"):
            _number(getattr(c, name), name)
        if c.min_coordinate >= c.max_coordinate:
            raise CuttingError("Invalid X3/Y3 soft range")
    if by_axis[5].pulses_per_unit != by_axis[6].pulses_per_unit:
        raise CuttingError("Raster requires equal X3/Y3 pulse scales")
    return values


def _pulses(point, calibrations):
    if len(point) != 2:
        raise CuttingError("Raster point must have exactly two coordinates")
    by_axis = dict(calibrations)
    result = []
    for axis, value in zip((5, 6), point):
        c = by_axis[axis]
        coordinate = _number(value, "raster coordinate") / 1000
        pulse = c.pulse_from_coordinate(coordinate)
        if not -(2**31) <= pulse < 2**31:
            raise CuttingError("Raster coordinate exceeds int32 pulse range")
        result.append(pulse)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class RasterLiveLine:
    """One board-coordinated lead-in / gated line / lead-out, not started yet."""
    path: LiveLinePath


@dataclass(frozen=True, slots=True)
class RasterRoute:
    plan: RasterPlan
    qualification: LiveQualification | None
    axis_calibrations: tuple[tuple[int, AxisCalibration], ...]
    watchdog_ms: int | None
    actions: tuple[RasterMove | DotExposure | RasterLiveLine, ...]
    path_endpoints_um: tuple[tuple[float, float], ...]
    preview_segments: tuple[tuple[tuple[float, float], tuple[float, float], bool], ...]
    exposure_points: tuple[tuple[float, float], ...]
    estimated_duration_s: float
    exposure_count: int

    def verify_integrity(self) -> None:
        """Rebuild from immutable provenance; no IO, status or qualification call."""
        proxy = SimpleNamespace(axis_calibrations=dict(self.axis_calibrations)) if self.axis_calibrations else None
        expected = build_raster_route(self.plan, self.qualification, proxy, self.watchdog_ms)
        if self != expected:
            raise CuttingError("Raster route does not match its complete planning provenance")


def _check_quantization(actions, calibrations, plan):
    if not calibrations:
        return
    speed = plan.settings.speed_um_s
    by_axis = dict(calibrations)
    exposures = []

    def pixel(pulse):
        pose = tuple(by_axis[axis].coordinate_from_pulse(value) * 1000
                     for axis, value in zip((5, 6), pulse))
        return plan.calibration.pixel_for_stage(pose, plan.reference_stage_um)

    dots, lines = set(), set()
    for action in actions:
        if isinstance(action, DotExposure):
            pulse = _pulses(action.point_um, calibrations)
            if pulse in dots:
                raise CuttingError("Distinct raster dots collapse onto the same pulse position")
            dots.add(pulse)
            exposures.append((pixel(pulse),))
        else:
            start, end = ((action.path.cut_start, action.path.cut_end)
                          if isinstance(action, RasterLiveLine) else (action.start_um, action.end_um))
            if isinstance(action, RasterLiveLine) or action.cut:
                pulse_line = (_pulses(start, calibrations), _pulses(end, calibrations))
                if pulse_line[0] == pulse_line[1]:
                    raise CuttingError("Raster cut line collapses to zero pulses")
                if pulse_line in lines or pulse_line[::-1] in lines:
                    raise CuttingError("Distinct raster rows collapse onto the same pulse line")
                lines.add(pulse_line)
                exposures.append(tuple(pixel(p) for p in pulse_line))
            if isinstance(action, RasterLiveLine):
                p = action.path
                points = tuple(_pulses(v, calibrations) for v in (p.lead_in, start, end, p.lead_out))
                dx, dy = (points[2][i] - points[1][i] for i in (0, 1))
                length = math.hypot(dx, dy)
                tangent = (dx / length, dy / length)
                projections = []
                for point in points:
                    offset = (point[0] - points[1][0], point[1] - points[1][1])
                    if abs(offset[0] * tangent[1] - offset[1] * tangent[0]) > math.sqrt(2) * CUTTING_POSITION_TOLERANCE_PULSE:
                        raise CuttingError("Quantized raster lead/cut points are not collinear")
                    projections.append(sum(v * t for v, t in zip(offset, tangent)))
                if any(b <= a for a, b in zip(projections, projections[1:])):
                    raise CuttingError("Quantized raster lead/cut ordering collapsed")
                velocity = speed * dict(calibrations)[5].pulses_per_unit / 1_000_000
                required = velocity**2 / (2 * CUTTING_ACCELERATION_PULSE_MS2)
                if min(projections[1] - projections[0], projections[3] - projections[2]) + 1e-9 < required:
                    raise CuttingError("Quantized raster leads are too short for acceleration")
    # Round-trip the actual integer commands through the same calibration used
    # for the snapshot. No pixel epsilon or pulse-sized ROI expansion is allowed.
    # For a concave polygon, valid endpoints do NOT imply a contained centerline.
    for points in exposures:
        if not all(plan.roi.contains(p) for p in points):
            raise CuttingError("Quantized raster exposure lies outside ROI")
        contained = (_inside(points[0], plan.polygon_pixels) if len(points) == 1
                     else _segment_inside(*points, plan.polygon_pixels))
        if not contained:
            raise CuttingError("Quantized raster exposure leaves polygon")


def build_raster_route(plan, qualification=None, controller=None, watchdog_ms=None) -> RasterRoute:
    """Pure, bounded route planning. ``controller`` may be a calibration proxy.

    Qualification records routing intent, not permission to execute. Passing no
    qualification to RasterRunner.run always rehearses this route DARK, even if
    this builder added live leads. Nominal time excludes USB/serial latency.
    """
    if not isinstance(plan, RasterPlan):
        raise CuttingError("A RasterPlan is required")
    plan.verify_integrity()
    if not 0 < len(plan.actions) <= MAX_ACTIONS or not 0 < plan.exposure_count <= MAX_EXPOSURES:
        raise CuttingError("Raster action/exposure limit exceeded")
    speed = _number(plan.settings.speed_um_s, "raster speed")
    if not 0 < speed <= CUTTING_MAX_SPEED_UM_S:
        raise CuttingError("Raster speed exceeds motion bounds")
    calibrations = _calibrations(controller)
    if qualification is not None:
        if not isinstance(qualification, LiveQualification) or not calibrations:
            raise CuttingError("Live route needs a qualification record and axis calibration values")
        if qualification.axis_configuration != axis_configuration(controller):
            raise CuttingError("Route qualification does not match axis calibration")
        if type(watchdog_ms) is not int or watchdog_ms <= 0:
            raise CuttingError("Live route needs an explicit firmware watchdog duration")
        if speed > qualification.max_speed_um_s:
            raise CuttingError("Raster speed exceeds commissioned speed")
    elif watchdog_ms is not None:
        raise CuttingError("A watchdog profile requires a qualification record")

    actions = []
    current = plan.reference_stage_um
    if qualification is not None and plan.settings.mode == "lines":
        for line in plan.lines:
            # Every selected pixel belongs to the ORIGINAL camera reference.
            # Rebasing to the previous row's endpoint would shift later rows.
            pixels = tuple(plan.calibration.pixel_for_stage(p, plan.reference_stage_um)
                           for p in (line.start_um, line.end_um))
            single = plan_line(*pixels, plan.reference_stage_um, plan.calibration,
                               plan.roi, plan.backlash, speed)
            path = plan_live_line(single, qualification, controller, watchdog_ms)
            for target in (path.preload, path.lead_in):
                if target != current:
                    actions.append(RasterMove(current, target, False))
                    current = target
            actions.append(RasterLiveLine(path))
            current = path.lead_out
            if len(actions) > MAX_ACTIONS:
                raise CuttingError("Expanded live raster exceeds action limit")
    else:
        actions = list(plan.actions)

    endpoints = [plan.reference_stage_um]
    segments, points, durations = [], [], []
    current = plan.reference_stage_um
    exposures = 0
    for action in actions:
        if isinstance(action, RasterMove):
            if action.start_um != current:
                raise CuttingError("Raster route is discontinuous")
            segments.append((action.start_um, action.end_um, action.cut))
            endpoints.append(action.end_um)
            durations.append(math.dist(current, action.end_um) / speed)
            current = action.end_um
            exposures += int(action.cut)
        elif isinstance(action, DotExposure):
            if action.point_um != current:
                raise CuttingError("Dot exposure is not at the current route position")
            for name in ("settle_ms", "dwell_ms"):
                value = getattr(action, name)
                if type(value) is not int or not (0 if name == "settle_ms" else 1) <= value <= 60000:
                    raise CuttingError("Dot timing must be bounded integer milliseconds")
            if qualification is not None and action.dwell_ms > qualification.max_window_ms:
                raise CuttingError("Dot dwell exceeds commissioned exposure window")
            points.append(action.point_um)
            endpoints.append(action.point_um)
            durations.append((action.settle_ms + action.dwell_ms) / 1000)
            exposures += 1
        elif isinstance(action, RasterLiveLine):
            p = action.path
            if p.lead_in != current:
                raise CuttingError("Live raster lead is discontinuous")
            for start, end, cut in ((p.lead_in, p.cut_start, False),
                                    (p.cut_start, p.cut_end, True), (p.cut_end, p.lead_out, False)):
                segments.append((start, end, cut))
                endpoints.append(end)
            current = p.lead_out
            durations.append(p.window_ms / 1000)
            exposures += 1
        else:
            raise CuttingError("Unknown raster action")
    duration = math.fsum(durations)
    if not actions or len(actions) > MAX_ACTIONS or exposures != plan.exposure_count or exposures > MAX_EXPOSURES:
        raise CuttingError("Invalid expanded raster action/exposure count")
    if not math.isfinite(duration) or not 0 < duration <= MAX_JOB_SECONDS:
        raise CuttingError("Raster nominal duration exceeds one hour")
    for point in endpoints:
        if len(point) != 2 or any(not math.isfinite(_number(v, "route coordinate")) for v in point):
            raise CuttingError("Invalid raster endpoint")
        if calibrations:
            _pulses(point, calibrations)
    _check_quantization(actions, calibrations, plan)
    return RasterRoute(plan, qualification, calibrations, watchdog_ms, tuple(actions), tuple(endpoints),
                       tuple(segments), tuple(points), duration, exposures)


class RasterRunner:
    """One-shot worker; one lease covers preflight, every move, settle and dwell.

    ``qualification=None`` is always dark. Physically inhibiting the laser is
    the caller/operator's prerequisite for a dark rehearsal. A connected laser
    is additionally commanded OFF; an absent device is never opened here.
    """
    def __init__(self, controller, laser=None):
        self.controller, self.laser = controller, laser
        self.owner = object()
        self.cancel = threading.Event()
        self.cleanup_required = False
        self.cleanup_error = None
        self._recovery_owner = None
        self._active = self._started = self._finished = self._abort_started = False
        self._abort_closed = False
        self._state_lock = threading.Lock()
        self._abort_tasks = []
        self._generation = None
        self._qualification = None
        self._guard = lambda: None
        self._deadline = math.inf
        self._calibration_values = ()
        self._laser_managed = False

    def abort(self):
        with self._state_lock:
            if self._finished or self._abort_closed:
                return  # A stale runner must not OFF a later job.
            self.cancel.set()
            if self._abort_started or not self._started:
                return
            self._abort_started = True
            targets = []
            if self._active and self._laser_managed:
                targets.append(self._abort_laser)
            if self._active:
                targets.append(self._abort_motion)
            for target in targets:
                task = threading.Thread(target=target, name="ATSRasterAbort", daemon=True)
                self._abort_tasks.append(task)
                task.start()

    def _abort_laser(self):
        try:
            self.laser.emergency_off()
        except Exception:
            pass  # Worker cleanup checks and reports acknowledgement failures.

    def _abort_motion(self):
        try:
            self.controller.stop_cutting(self.owner)
        except Exception:
            pass

    def _check(self):
        if self.cancel.is_set():
            raise CuttingError("Raster cancelled")
        if time.monotonic() >= self._deadline:
            raise CuttingError("Raster job deadline exceeded")
        self._guard()

    def _dark_ack(self, status, *, startup=False, inactive=False):
        if status.state.name != "DISARMED" or status.comp_high is not False or status.fault is not None:
            raise CuttingError("Laser OFF acknowledgement is not confirmed")
        if startup and status.startup_checked is not True:
            raise CuttingError("Laser startup check is no longer confirmed")
        if inactive and status.trigger_active is not False:
            raise CuttingError("Board trigger must be inactive")

    def _off(self):
        self._check()
        status = self.laser.off()
        self._dark_ack(status, startup=self._qualification is not None)
        if self._generation is not None and status.stop_generation != self._generation + 1:
            raise CuttingError("Laser stop generation changed outside this raster job")
        self._generation = status.stop_generation
        return status

    def _health(self, expected):
        status = self.laser.check_ready(require_trigger=True)
        self._qualification.require_laser(status)
        if status.stop_generation != self._generation or status.state.name != expected or status.fault is not None:
            raise CuttingError("Unexpected laser state or stop generation during raster")
        if expected == "DISARMED":
            self._dark_ack(status, startup=True)
        return status

    def _stationary(self, point):
        if not self.controller.cutting_move_status(self.owner):
            raise CuttingError("Stage moved during stationary dot exposure")
        pose = self.controller.get_cutting_pose()
        cals = dict(self._calibration_values)
        for axis, actual, wanted in zip((5, 6), pose, _pulses(point, self._calibration_values)):
            c = cals[axis]
            raw = c.zero_pulse + (actual / 1000 - c.zero_coordinate) * c.pulses_per_unit / c.pulse_sign
            if not math.isfinite(raw) or abs(raw - wanted) > CUTTING_POSITION_TOLERANCE_PULSE:
                raise CuttingError("Planner position changed during dot exposure")

    def _move(self, target, speed):
        self._check()
        if self._qualification is not None:
            self._health("DISARMED")
        pose = self.controller.get_cutting_pose()
        if _pulses(pose, self._calibration_values) == _pulses(target, self._calibration_values):
            self._stationary(target)
            return
        self.controller.start_cutting_move(self.owner, target, speed)
        deadline = min(self._deadline, time.monotonic() + max(3., math.dist(pose, target) / speed * 3 + 2))
        self._wait_motion(deadline)

    def _wait_motion(self, deadline, *, exposing=False):
        next_health = 0.
        while True:
            self._check()
            now = time.monotonic()
            if now >= deadline:
                raise CuttingError("Raster motion timed out")
            if self._qualification is not None and now >= next_health:
                self._health("FOLLOW" if exposing else "DISARMED")
                if exposing:
                    self.laser.keepalive(expected_stop_generation=self._generation)
                next_health = now + _KEEP_SECONDS
            if self.controller.cutting_move_status(self.owner):
                return
            self.cancel.wait(_POLL_SECONDS)

    def _wait_dark(self, duration, point):
        until = time.monotonic() + duration
        while True:
            self._check()
            self._stationary(point)
            if self._qualification is not None:
                self._health("DISARMED")
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            self.cancel.wait(min(_POLL_SECONDS, remaining))

    def _line(self, action, speed):
        p = action.path
        if self._qualification is None:
            for target in (p.cut_start, p.cut_end, p.lead_out):
                self._move(target, speed)
            return
        self._check()
        self.controller.prepare_cutting_line(self.owner, p.cut_start, p.cut_end,
                                             p.lead_out, speed, self._qualification.output)
        self._check()
        self._dark_ack(self._health("DISARMED"), startup=True, inactive=True)
        self.laser.arm(expected_stop_generation=self._generation)
        self._check()
        self.laser.keepalive(expected_stop_generation=self._generation)
        self._check()
        follow = self.laser.follow(p.window_ms, expected_stop_generation=self._generation)
        if follow.state.name != "FOLLOW" or follow.comp_high is not False or follow.trigger_active is not False:
            raise CuttingError("FOLLOW did not acknowledge an inactive trigger")
        self._check()
        self.laser.keepalive(expected_stop_generation=self._generation)
        self._check()
        self.controller.start_prepared_cutting_line(self.owner)
        self._wait_motion(min(self._deadline, time.monotonic() + p.window_ms / 1000), exposing=True)
        self._off()

    def _fire_completed(self, token, status):
        """Only a driver-attributed natural completion may advance generation.

        DISARMED alone is insufficient: an external OFF can look identical.
        The driver record binds the accepted FIRE and its original generation.
        """
        record = self.laser.get_fire_completion(token)
        if record is None:
            return False
        sequence, generation = token.fire_sequence, token.started_generation
        boot, session = token.boot_nonce, token.session_nonce
        return (
            record.fire_sequence == sequence
            and record.started_generation == generation
            and record.completed_generation == generation + 1
            and record.boot_nonce == boot == status.boot_nonce
            and record.session_nonce == session == status.session_nonce
            and status.stop_generation == record.completed_generation
            and status.state.name == "DISARMED" and status.comp_high is False
            and status.fault is None and status.startup_checked is True
            and status.window_remaining_ms == 0
        )

    def _dot(self, action):
        self._wait_dark(action.settle_ms / 1000, action.point_um)
        if self._qualification is None:
            self._wait_dark(action.dwell_ms / 1000, action.point_um)
            return
        self._dark_ack(self._health("DISARMED"), startup=True, inactive=True)
        self._check()
        self.laser.arm(expected_stop_generation=self._generation)
        self._check()
        self.laser.keepalive(expected_stop_generation=self._generation)
        self._check()
        deadline = min(self._deadline, time.monotonic() + max(3., action.dwell_ms / 1000 * 3 + 2))
        # Sequence allocation and receipt capture belong to the driver. A queued
        # monitor STATUS can consume a sequence between any two runner calls.
        status = self.laser.fire(action.dwell_ms, expected_stop_generation=self._generation)
        token = getattr(status, "fire_token", None)
        if token is None or token.started_generation != self._generation:
            raise CuttingError("FIRE did not return its actual command receipt")
        next_keep = time.monotonic() + _KEEP_SECONDS
        while True:
            self._check()
            self._stationary(action.point_um)
            if self._fire_completed(token, status):
                self._qualification.require_laser(status)
                self._generation = status.stop_generation
                break
            if (status.state.name != "FIRING" or status.fault is not None
                    or status.stop_generation != self._generation or status.comp_high is not True
                    or status.boot_nonce != token.boot_nonce or status.session_nonce != token.session_nonce):
                raise CuttingError("FIRE ended without a matching natural completion")
            if time.monotonic() >= deadline:
                raise CuttingError("Dot FIRE completion timed out")
            self._qualification.require_laser(status)
            try:
                # Poll before KEEP. A natural expiry can cancel STATUS/KEEP in
                # flight; inspect the driver's completion record in that case.
                status = self.laser.poll()
                if self._fire_completed(token, status):
                    continue
                if status.stop_generation != self._generation or status.state.name != "FIRING":
                    raise CuttingError("Dot interrupted by stop/fault")
                if time.monotonic() >= next_keep:
                    status = self.laser.keepalive(expected_stop_generation=self._generation)
                    next_keep = time.monotonic() + _KEEP_SECONDS
            except Exception:
                status = self.laser.status
                if not self._fire_completed(token, status):
                    raise
            self.cancel.wait(_POLL_SECONDS)
        self._check()
        self._off()  # Acknowledged OFF before any subsequent stage move.

    def _stop_confirmed(self, owner):
        status = self.controller.get_cutting_lease_status(owner)
        if not status.stop_confirmed or status.cleanup_required:
            try:
                self.controller.stop_cutting(owner)
            except Exception:
                # An independent abort/global stop may have won this exact lease.
                status = self.controller.get_cutting_lease_status(owner)
                if not status.stop_confirmed or status.cleanup_required:
                    raise
        status = self.controller.get_cutting_lease_status(owner)
        if not status.stop_confirmed or status.cleanup_required:
            raise CuttingError("Raster stop unconfirmed; motion remains interlocked")

    def _hold_cleanup_interlock(self, errors):
        owner = self._recovery_owner or self.owner
        try:
            status = self.controller.get_cutting_lease_status(owner)
            if status.valid or status.cleanup_required:
                return
            # Stop invalidates the original owner. If serial OFF is unconfirmed,
            # take a NEW, non-executing lease rather than release motion to a new
            # job while a stationary FIRE may still be active. Global stop remains
            # available; no stale owner is allowed to stop another lease.
            recovery = object()
            self.controller.acquire_cutting(recovery)
            self._recovery_owner = recovery
        except Exception as error:
            errors.append(f"recovery lease unavailable: {error}")

    def _cleanup(self):
        errors = []
        if self._laser_managed:
            try:
                status = self.laser.emergency_off()
                if status.comp_high is not False or status.state.name not in ("DISARMED", "FAULT"):
                    raise CuttingError("laser OFF acknowledgement is not confirmed")
            except Exception as error:
                errors.append(f"laser OFF unconfirmed: {error}")
        try:
            self._stop_confirmed(self._recovery_owner or self.owner)
        except Exception as error:
            errors.append(f"motion cleanup: {error}")
        # Do not let a delayed old abort command overtake a future job.
        until = time.monotonic() + .5
        for task in self._abort_tasks:
            task.join(max(0., until - time.monotonic()))
        if any(t.is_alive() for t in self._abort_tasks):
            errors.append("independent abort worker has not finished")
        if errors:
            self._hold_cleanup_interlock(errors)
        else:
            self._recovery_owner = None
        self.cleanup_required = bool(errors)
        self.cleanup_error = "; ".join(errors) if errors else None
        return errors

    def retry_cleanup(self):
        """Worker-thread recovery only; never resumes or re-arms the failed job."""
        if not self._finished:
            raise CuttingError("Raster execution is still active")
        if self.cleanup_required:
            status = self.controller.get_cutting_lease_status(self._recovery_owner or self.owner)
            if not (status.valid or status.stop_confirmed or status.can_retry_stop):
                self.cleanup_error = "stale recovery owner; cannot affect another job"
                raise CuttingError(self.cleanup_error)
            if status.stop_confirmed:
                # A global STOP may have released our recovery lease. Reserve
                # motion again BEFORE serial cleanup; a newer owner must block
                # this stale runner's OFF as well as its board stop.
                errors = []
                self._hold_cleanup_interlock(errors)
                if errors:
                    self.cleanup_error = "; ".join(errors)
                    raise CuttingError(self.cleanup_error)
            errors = self._cleanup()
            if errors:
                raise CuttingError("; ".join(errors))

    def run(self, plan, snapshot, context, progress=lambda *_: None, *, calibration=None,
            route=None, qualification=None, startup_confirmed=False, reference_guard=lambda: None):
        with self._state_lock:
            if self._started:
                raise CuttingError("RasterRunner is single-use; create a new runner")
            self._started = True
        started = time.monotonic()
        self._deadline = started + MAX_JOB_SECONDS
        self._guard = reference_guard
        self._qualification = qualification
        self._laser_managed = self.laser is not None and (
            qualification is not None or getattr(self.laser, "connected", False) is True)
        acquired = False
        try:
            self._check()
            self.controller.acquire_cutting(self.owner)
            acquired = self._active = True
            self._check()
            snapshot.validate(self.controller, context)
            plan.validate_for(calibration if calibration is not None else plan.calibration,
                              context, snapshot.stage_um)
            self._calibration_values = _calibrations(self.controller)
            watchdog = None
            if qualification is not None:
                if not isinstance(qualification, LiveQualification) or startup_confirmed is not True or self.laser is None:
                    raise CuttingError("Live raster requires qualification, laser and startup confirmation")
                qualification.require_qualified(self.controller)
                self._laser_managed = True
                status = self.laser.check_ready(require_trigger=True)
                qualification.require_laser(status)
                self._dark_ack(status, startup=True)
                self._generation = status.stop_generation
                watchdog = status.watchdog_ms
                if plan.settings.mode == "dots" and not all(callable(getattr(self.laser, name, None)) for name in
                        ("fire", "get_fire_completion")):
                    raise CuttingError("Live dots require driver-attributed natural FIRE completion capability")
            else:
                self._laser_managed = self.laser is not None and getattr(self.laser, "connected", False) is True
                if self._laser_managed:
                    self._generation = self.laser.status.stop_generation
            if route is None:
                route = build_raster_route(plan, qualification, self.controller, watchdog)
            if not isinstance(route, RasterRoute) or route.plan != plan:
                raise CuttingError("Raster route belongs to a different plan")
            route.verify_integrity()
            if route.axis_calibrations and route.axis_calibrations != self._calibration_values:
                raise CuttingError("Raster route axis calibration is stale")
            if qualification is not None and (route.qualification != qualification or route.watchdog_ms != watchdog):
                raise CuttingError("Live execution requires the exact qualified preview route")
            _check_quantization(route.actions, self._calibration_values, plan)
            self.controller.validate_cutting_path(self.owner, route.path_endpoints_um)
            if not self.controller.cutting_move_status(self.owner):
                raise CuttingError("Raster preflight requires stationary axes")
            # Nominal geometry time excludes per-dot serial ACK latency. Dense
            # short exposures must not inherit a tiny multiple of that estimate.
            # The whole job still has a hard monotonic one-hour deadline, with
            # tighter motion/FIRE deadlines inside each action.
            self._deadline = started + MAX_JOB_SECONDS
            self._check()
            if self._laser_managed:
                self._off()
            total = len(route.actions)
            for index, action in enumerate(route.actions):
                self._check()
                if isinstance(action, RasterMove):
                    if qualification is not None and action.cut:
                        raise CuttingError("Live cut requires a prepared board-coordinated line")
                    self._move(action.end_um, plan.settings.speed_um_s)
                elif isinstance(action, RasterLiveLine):
                    self._line(action, plan.settings.speed_um_s)
                else:
                    self._dot(action)
                self._check()
                progress(index + 1, total)
            self._check()
            # All motion and managed laser OFF acknowledgements are complete.
            # Atomically choose success or cancellation BEFORE releasing motion
            # to another job. Never hold this lock across a DLL transaction.
            with self._state_lock:
                self._abort_closed = True
                if self.cancel.is_set():
                    raise CuttingError("Raster cancelled")
            self.controller.release_cutting(self.owner)
        except BaseException as fault:
            # Freeze the abort task set before cleanup joins it. Otherwise an
            # abort could enqueue a late, unscoped OFF after cleanup finished.
            with self._state_lock:
                self._abort_closed = True
            if acquired:
                errors = self._cleanup()
                if errors:
                    raise CuttingError(f"{fault}; {'; '.join(errors)}") from fault
            raise
        finally:
            with self._state_lock:
                self._abort_closed = True
                self._active = False
                self._finished = True
