"""Qualified board-triggered cutting; no host-timed opening of a moving beam.

The apparatus supplied for this feature has no independent inhibit. Its default
firmware capabilities and the absence of a commissioning record therefore keep
live execution unavailable. Tests inject fake devices, never qualification of
the real apparatus.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from numbers import Real
import threading
import time

from .axis_calibration import AxisCalibration
from .cutting_geometry import CuttingPlan
from .cutting_session import CuttingError, axis_configuration
from .gas_controller import CUTTING_ACCELERATION_PULSE_MS2, CUTTING_MAX_SPEED_UM_S


def _finite(value, name, *, positive=True):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise ValueError(f"{name} is outside its allowed range")
    return value


@dataclass(frozen=True)
class LiveQualification:
    evidence_id: str
    axis_configuration: str
    output: object
    independent_inhibit_verified: bool
    board_io_verified: bool
    timing_verified: bool
    max_gate_latency_ms: float
    max_path_error_um: float
    lead_distance_um: float
    max_speed_um_s: float
    max_loss_exposure_ms: int
    max_window_ms: int

    def __post_init__(self):
        from .gas_controller import CuttingGateOutput
        if not isinstance(self.output, CuttingGateOutput):
            raise ValueError("an explicitly mapped CuttingGateOutput is required")
        for name in ("evidence_id", "axis_configuration"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        for name in ("independent_inhibit_verified", "board_io_verified", "timing_verified"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be an explicit boolean")
        for name in ("max_gate_latency_ms", "max_path_error_um", "lead_distance_um", "max_speed_um_s"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if self.max_speed_um_s > CUTTING_MAX_SPEED_UM_S:
            raise ValueError("commissioned speed exceeds the controller limit")
        for name in ("max_loss_exposure_ms", "max_window_ms"):
            if type(getattr(self, name)) is not int or not 1 <= getattr(self, name) <= 60000:
                raise ValueError(f"{name} must be an integer within 1..60000 ms")
        if self.max_loss_exposure_ms > self.max_window_ms:
            raise ValueError("loss exposure bound exceeds the maximum exposure window")

    def to_dict(self):
        return {"schema_version": 1, **asdict(self)}

    @classmethod
    def from_dict(cls, data):
        from .gas_controller import CuttingGateOutput
        keys = set(cls.__dataclass_fields__) | {"schema_version"}
        if not isinstance(data, dict) or set(data) != keys or type(data["schema_version"]) is not int or data["schema_version"] != 1:
            raise ValueError("unsupported or incomplete laser commissioning record")
        values = dict(data)
        values.pop("schema_version")
        if not isinstance(values["output"], dict):
            raise ValueError("commissioning record has no mapped gate output")
        values["output"] = CuttingGateOutput(**values["output"])
        return cls(**values)

    @classmethod
    def template(cls, controller):
        """Deliberately incomplete: no guessed output bit or timing qualification."""
        return {
            "schema_version": 1, "evidence_id": "",
            "axis_configuration": axis_configuration(controller), "output": {
                "card_index": None, "bit_index": None, "buf_do_type": None, "active_high": None,
            }, "independent_inhibit_verified": False,
            "board_io_verified": False, "timing_verified": False,
            "max_gate_latency_ms": None, "max_path_error_um": None,
            "lead_distance_um": None, "max_speed_um_s": None,
            "max_loss_exposure_ms": None, "max_window_ms": None,
        }

    def require_qualified(self, controller):
        if not all((self.independent_inhibit_verified, self.board_io_verified, self.timing_verified)):
            raise CuttingError("Independent inhibit, board IO and timing commissioning are required")
        if self.axis_configuration != axis_configuration(controller):
            raise CuttingError("Commissioning does not match the current axis configuration")
        if not controller.cutting_qualified:
            raise CuttingError("Motion interpolation is not qualified for this connection")
        if controller.qualified_cutting_gate != self.output:
            raise CuttingError("Gate IO is not qualified for this connection")

    def require_laser(self, status):
        if not (status.interlock_wired and status.interlock_ok and status.trigger_wired):
            raise CuttingError("Independent inhibit and board trigger inputs are required")
        watchdog = status.watchdog_ms
        if type(watchdog) is not int or watchdog <= 0:
            raise CuttingError("Firmware has no known active watchdog")
        if watchdog + self.max_gate_latency_ms > self.max_loss_exposure_ms:
            raise CuttingError("Firmware watchdog exceeds the commissioned loss-exposure bound")
        # The provided firmware uses a pulled-up, active-low trigger. A mapping
        # that opens on HIGH would invert the queued gate and is rejected.
        if self.output.active_high:
            raise CuttingError("Firmware requires an active-low board trigger")


@dataclass(frozen=True)
class LiveLinePath:
    reference: tuple[float, float]
    preload: tuple[float, float]
    lead_in: tuple[float, float]
    cut_start: tuple[float, float]
    cut_end: tuple[float, float]
    lead_out: tuple[float, float]
    window_ms: int

    @property
    def endpoints(self):
        return (self.reference, self.preload, self.lead_in, self.cut_start, self.cut_end, self.lead_out)


def plan_live_line(plan: CuttingPlan, qualification: LiveQualification, controller, watchdog_ms: int) -> LiveLinePath:
    """Dark lead-in/out accelerate/decelerate outside the nominal cut endpoints."""
    if type(watchdog_ms) is not int or watchdog_ms <= 0:
        raise CuttingError("Unknown firmware watchdog duration")
    speed = _finite(plan.speed_um_s, "speed_um_s")
    if speed > qualification.max_speed_um_s:
        raise CuttingError("Path speed exceeds the commissioned laser speed")
    if speed * qualification.max_gate_latency_ms / 1000 > qualification.max_path_error_um:
        raise CuttingError("Gate latency exceeds the allowed spatial error at this speed")
    scales = tuple(controller.axis_calibrations[a].pulses_per_unit for a in (5, 6))
    if scales[0] != scales[1]:
        raise CuttingError("Coordinated laser cutting requires equal X3/Y3 scales")
    acceleration = CUTTING_ACCELERATION_PULSE_MS2 * 1e9 / _finite(scales[0], "pulse/mm")
    # Two endpoint quantizations plus the board's confirmed-position tolerance
    # must not consume the entire theoretical acceleration distance.
    from .gas_controller import CUTTING_POSITION_TOLERANCE_PULSE
    quantization_margin = (1 + CUTTING_POSITION_TOLERANCE_PULSE) * math.sqrt(2) * 1000 / scales[0]
    minimum_lead = speed * speed / (2 * acceleration) + quantization_margin
    lead = qualification.lead_distance_um
    if lead < minimum_lead:
        raise CuttingError("Dark lead distance is shorter than the acceleration/deceleration distance")
    cut = [s for s in plan.segments if s.cut]
    if len(cut) != 1:
        raise CuttingError("Live line execution requires exactly one cut segment")
    start, end = cut[0].start_um, cut[0].end_um
    # Validate actual commanded pulse endpoints, not just the selected pixels.
    # A straight segment stays in this convex ROI iff both endpoints do. Reject
    # thin/boundary paths rather than enlarging the ROI by a pulse tolerance.
    # Preview supplies immutable value-only proxies, not controller methods.
    calibrations = tuple(AxisCalibration(**{
        name: getattr(controller.axis_calibrations[a], name)
        for name in AxisCalibration.__dataclass_fields__
    }) for a in (5, 6))
    for point in (start, end):
        quantized = tuple(c.coordinate_from_pulse(c.pulse_from_coordinate(v / 1000)) * 1000
                          for c, v in zip(calibrations, point))
        if not plan.roi.contains(plan.calibration.pixel_for_stage(quantized, plan.reference_stage_um)):
            raise CuttingError("Quantized cutting line endpoint lies outside ROI")
    length = math.dist(start, end)
    direction = tuple((b - a) / length for a, b in zip(start, end))
    lead_in = tuple(a - lead * d for a, d in zip(start, direction))
    lead_out = tuple(b + lead * d for b, d in zip(end, direction))
    preload = tuple(p - offset for p, offset in zip(lead_in, plan.backlash_offset_um))
    # Includes the two ramps and a bounded host-start allowance no greater than
    # one firmware watchdog period. The MCU independently limits this window.
    duration_ms = ((length + 2 * lead) / speed + speed / acceleration) * 1000 + watchdog_ms
    if not math.isfinite(duration_ms) or duration_ms > qualification.max_window_ms:
        raise CuttingError("Cut duration exceeds the commissioned emission window")
    if not all(math.isfinite(v) for point in (preload, lead_in, lead_out) for v in point):
        raise CuttingError("Non-finite dark lead path")
    return LiveLinePath(plan.reference_stage_um, preload, lead_in, start, end, lead_out, math.ceil(duration_ms))


class LiveCuttingRunner:
    """Serial OFF is independent of the motion-card DLL and its lock.

    Keepalives are emitted only by the job's progress loop. A stalled DLL cannot
    leave a separate heartbeat thread sustaining emission indefinitely.
    """

    def __init__(self, controller, laser):
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
        self._stop_generation = None

    def abort(self):
        with self._state_lock:
            if self._finished or self._abort_closed:
                return
            self.cancel.set()
            if self._abort_started or not self._active:
                return
            self._abort_started = True
            for target, name in ((self._off_best_effort, "ATSLaserAbort"),
                                 (self._stop_best_effort, "ATSLiveMotionAbort")):
                task = threading.Thread(target=target, name=name, daemon=True)
                self._abort_tasks.append(task)
                task.start()

    def _off_best_effort(self):
        try:
            self.laser.emergency_off()
        except Exception:
            pass  # Worker reports the final acknowledged state/cleanup failure.

    def _stop_best_effort(self):
        try:
            self.controller.stop_cutting(self.owner)
        except Exception:
            pass

    def _check_cancel(self, reference_guard):
        if self.cancel.is_set():
            raise CuttingError("Cutting cancelled")
        reference_guard()

    def _wait_motion(self, deadline, qualification, reference_guard, *, exposing=False):
        next_check = 0.
        while True:
            self._check_cancel(reference_guard)
            now = time.monotonic()
            if now >= deadline:
                raise CuttingError("Cutting motion timed out")
            if now >= next_check:
                if exposing:
                    self.laser.keepalive(expected_stop_generation=self._stop_generation)
                status = self.laser.check_ready(require_trigger=True)
                qualification.require_laser(status)
                if status.stop_generation != self._stop_generation:
                    raise CuttingError("Laser stop generation changed; old job cannot resume")
                expected = "FOLLOW" if exposing else "DISARMED"
                if status.state.name != expected:
                    raise CuttingError(f"Unexpected laser state during motion: {status.state.name}")
                next_check = now + .075
            if self.controller.cutting_move_status(self.owner):
                return
            self.cancel.wait(.02)

    def run(self, plan, snapshot, context, qualification, progress=lambda *_: None,
            *, calibration, startup_confirmed=False, reference_guard=lambda: None):
        with self._state_lock:
            if self._started:
                raise CuttingError("LiveCuttingRunner is single-use; create a new runner")
            self._started = True
        acquired = False
        try:
            self._check_cancel(reference_guard)
            if startup_confirmed is not True:
                raise CuttingError("Confirm laser self-test, temperature and CDRH delay completion first")
            qualification.require_qualified(self.controller)
            status = self.laser.check_ready(require_trigger=True)
            qualification.require_laser(status)
            route = plan_live_line(plan, qualification, self.controller, status.watchdog_ms)
            self._check_cancel(reference_guard)
            self.controller.acquire_cutting(self.owner)
            acquired = self._active = True
            self._check_cancel(reference_guard)
            snapshot.validate(self.controller, context)
            plan.validate_for(calibration, context, snapshot.stage_um)
            qualification.require_qualified(self.controller)
            self.controller.validate_cutting_path(self.owner, route.endpoints)
            self._check_cancel(reference_guard)
            status = self.laser.off()
            if status.state.name != "DISARMED" or status.comp_high is not False or not status.startup_checked:
                raise CuttingError("Laser OFF/startup acknowledgement is not confirmed")
            self._stop_generation = status.stop_generation
            for index, target in enumerate((route.preload, route.lead_in)):
                self._check_cancel(reference_guard)
                pose = self.controller.get_cutting_pose()
                duration = math.dist(pose, target) / plan.speed_um_s
                if duration > 3600:
                    raise CuttingError("Dark travel exceeds one hour")
                if math.dist(pose, target) > 1e-9:
                    self.controller.start_cutting_move(self.owner, target, plan.speed_um_s)
                    self._wait_motion(time.monotonic() + max(3., duration * 3 + 2), qualification, reference_guard)
                progress(index + 1, 3)
            self._check_cancel(reference_guard)
            self.controller.prepare_cutting_line(self.owner, route.cut_start, route.cut_end,
                                                  route.lead_out, plan.speed_um_s, qualification.output)
            self._check_cancel(reference_guard)
            status = self.laser.check_ready(require_trigger=True)
            qualification.require_laser(status)
            if status.state.name != "DISARMED" or status.trigger_active is not False:
                raise CuttingError("Board trigger must be inactive before arming FOLLOW")
            self.laser.arm(expected_stop_generation=self._stop_generation)
            self.laser.keepalive(expected_stop_generation=self._stop_generation)
            self.laser.follow(route.window_ms, expected_stop_generation=self._stop_generation)
            self._check_cancel(reference_guard)
            self.laser.keepalive(expected_stop_generation=self._stop_generation)
            self.controller.start_prepared_cutting_line(self.owner)
            self._wait_motion(time.monotonic() + route.window_ms / 1000,
                              qualification, reference_guard, exposing=True)
            status = self.laser.off()
            if status.stop_generation != self._stop_generation + 1:
                raise CuttingError("Laser stop generation changed outside this cutting job")
            if status.state.name != "DISARMED" or status.comp_high is not False:
                raise CuttingError("Laser OFF acknowledgement is not confirmed")
            progress(3, 3)
            self._check_cancel(reference_guard)
            # Serial OFF and motion completion are confirmed before closing the
            # abort gate. Release may block in the DLL; keep it outside the lock.
            with self._state_lock:
                self._abort_closed = True
                if self.cancel.is_set():
                    raise CuttingError("Cutting cancelled")
            self.controller.release_cutting(self.owner)
        except BaseException as fault:
            with self._state_lock:
                self._abort_closed = True
            # A preflight/acquisition failure never owned the laser job. In
            # particular it must not send an unscoped OFF to a competing owner.
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

    def _stop_confirmed(self, owner):
        status = self.controller.get_cutting_lease_status(owner)
        if not status.stop_confirmed or status.cleanup_required:
            try:
                self.controller.stop_cutting(owner)
            except Exception:
                status = self.controller.get_cutting_lease_status(owner)
                if not status.stop_confirmed or status.cleanup_required:
                    raise
        status = self.controller.get_cutting_lease_status(owner)
        if not status.stop_confirmed or status.cleanup_required:
            raise CuttingError("motion stop unconfirmed")

    def _hold_cleanup_interlock(self, errors):
        owner = self._recovery_owner or self.owner
        try:
            status = self.controller.get_cutting_lease_status(owner)
            if status.valid or status.cleanup_required:
                return
            # A successful stop invalidates the original token. Reserve a new
            # non-executing lease until serial OFF and all abort tasks finish.
            recovery = object()
            self.controller.acquire_cutting(recovery)
            self._recovery_owner = recovery
        except Exception as error:
            errors.append(f"recovery lease unavailable: {error}")

    def _cleanup(self):
        errors = []
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
        until = time.monotonic() + .5
        for task in self._abort_tasks:
            task.join(max(0., until - time.monotonic()))
        if any(task.is_alive() for task in self._abort_tasks):
            errors.append("independent abort worker has not finished")
        if errors:
            self._hold_cleanup_interlock(errors)
        else:
            self._recovery_owner = None
        self.cleanup_required = bool(errors)
        self.cleanup_error = "; ".join(errors) if errors else None
        return errors

    def retry_cleanup(self):
        """Worker-thread OFF/stop recovery only; never resumes the old job."""
        if not self._finished:
            raise CuttingError("Live execution is still active")
        if self.cleanup_required:
            status = self.controller.get_cutting_lease_status(self._recovery_owner or self.owner)
            if not (status.valid or status.stop_confirmed or status.can_retry_stop):
                self.cleanup_error = "stale recovery owner; cannot affect another job"
                raise CuttingError(self.cleanup_error)
            if status.stop_confirmed:
                # Global STOP may have released recovery. Reacquire BEFORE any
                # serial OFF; a newer owner must prevent stale cleanup entirely.
                errors = []
                self._hold_cleanup_interlock(errors)
                if errors:
                    self.cleanup_error = "; ".join(errors)
                    raise CuttingError(self.cleanup_error)
            errors = self._cleanup()
            if errors:
                raise CuttingError("; ".join(errors))
