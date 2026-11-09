from __future__ import annotations

from functools import wraps
from dataclasses import dataclass
import logging
import math
import threading
import time
from types import MappingProxyType

from .i18n import tr
from .controller_base import (
    ALL_AXES,
    AXIS9_DEGREES_PER_PULSE,
    AXIS9_MAX_DEGREES,
    AXIS9_MAX_VELOCITY,
    AXIS9_MIN_DEGREES,
    AXIS9_NUM,
    AXIS_HARD_LIMITS,
    AXES_WITHOUT_LIMITS,
    BOARD_IP,
    LIMIT_TYPE_NEGATIVE,
    LIMIT_TYPE_POSITIVE,
    OPEN_MODE,
    MotionControllerError,
    axis_mask,
)
from .axis_calibration import (
    AXIS_CALIBRATIONS,
    MICROSCOPE_Z_MAX_ABS_PULSE,
    _round_half_up,
    axis_has_absolute_calibration,
    axis_has_finite_soft_range,
    coordinate_from_pulse,
    is_target_safe,
    pulse_from_coordinate,
    safe_continuous_target,
)
from .gas_api import AXIS_STATUS_NEG_HARD_LIMIT, AXIS_STATUS_POS_HARD_LIMIT, GasApi, TCrdPrm


_log = logging.getLogger("ats.motion")
_audit = logging.getLogger("ats.audit")

AXIS_STATUS_RUNNING = 0x00000400
AXIS_STATUS_ARRIVE = 0x00000800
AXIS_STATUS_HOME_RUNNING = 0x00001000
# GAS_N.h: alarm, software limits, following error, hard limits, stop inputs.
CUTTING_AXIS_FAULT_MASK = 0x000001FE
CUTTING_AXES = (5, 6)
CUTTING_AXIS_MASK = axis_mask(5) | axis_mask(6)
CUTTING_CRD = 1
CUTTING_FIFO = 0
CUTTING_POSITION_TOLERANCE_PULSE = 1.0
CUTTING_ACCELERATION_PULSE_MS2 = 0.1
CUTTING_MAX_SPEED_UM_S = 5000.0


@dataclass(frozen=True)
class CuttingGateOutput:
    """An explicitly mapped single external bit; no pin/type/polarity defaults.

    Qualification must establish that BufIO's type/card/mask addresses exactly
    the same physical output as Set/GetExtDoBit. A register readback is neither
    optical emission feedback nor an independent safety inhibit.
    """

    card_index: int
    bit_index: int
    buf_do_type: int
    active_high: bool

    def __post_init__(self):
        for name, maximum in (("card_index", 0x7FFF), ("bit_index", 15), ("buf_do_type", 0xFFFF)):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError(f"{name} must be an integer in 0..{maximum}")
        if type(self.active_high) is not bool:
            raise ValueError("active_high must be an explicit bool")

    @property
    def mask(self) -> int:
        return 1 << self.bit_index

    @property
    def off_level(self) -> int:
        return 0 if self.active_high else 1


@dataclass(frozen=True)
class _PreparedCuttingLine:
    output: CuttingGateOutput
    targets_um: tuple[tuple[float, float], ...]
    reference: tuple[float, ...]
    final_target: tuple[int, int]


@dataclass(frozen=True)
class CuttingLeaseStatus:
    """Cached lease state, not hardware telemetry. Query motion on a worker."""

    valid: bool
    generation: int
    state: str
    reason: str | None
    qualified: bool
    cleanup_required: bool
    stop_confirmed: bool
    can_retry_stop: bool


AXIS9_ZERO_SWITCH_CARD_INDEX = 0
AXIS9_ZERO_SWITCH_IO_INDEX = 8
AXIS9_ZERO_SWITCH_ACTIVE_VALUE = 1
AXIS9_HOME_SEARCH_MIN_DEGREES = -10.0
AXIS9_HOME_SEARCH_MAX_DEGREES = 10.0
AXIS9_HOME_PREPOSITION_SPEED_DEG_S = 2.0
AXIS9_HOME_PREPOSITION_TIMEOUT_MARGIN_S = 10.0
AXIS9_HOME_IO_SEARCH_SPEED_DEG_S = 0.25
AXIS9_HOME_IO_SEARCH_TIMEOUT_MARGIN_S = 10.0


def _api_locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._api_lock:
            return method(self, *args, **kwargs)

    return wrapper


def _manual_mutation(method):
    """Serialize and guard every manual/gamepad mutation, even non-sample axes."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._api_lock:
            self._guard_cutting()
            # Count attempts conservatively: a failed DLL transaction may have
            # partially changed planner/configuration state.
            self._motion_revision += 1
            return method(self, *args, **kwargs)
    return wrapper


class Axis9HomeJob:
    """One homing attempt. poll() executes at most one short, serialized card transaction."""
    def __init__(self, controller, generation):
        self.controller = controller
        self.generation = generation
        self.phase = "start"
        self.deadline = 0.0

    def poll(self) -> float | None:
        """Return the switch pulse on success, None while moving; raise on cancellation/fault."""
        return self.controller._poll_axis9_home(self)


class GasMotionController:
    def __init__(
        self,
        api: GasApi,
        board_ip: str = BOARD_IP,
        open_mode: int = OPEN_MODE,
        axis9_degrees_per_pulse: float = AXIS9_DEGREES_PER_PULSE,
        *,
        cutting_qualified: bool = False,
    ):
        self.api = api
        self.board_ip = board_ip
        self.open_mode = open_mode
        self.axis9_degrees_per_pulse = axis9_degrees_per_pulse
        self.axis3_marked_position = 0.0
        # Per-axis last-committed RELATIVE-step target (pulses). Lets rapid step
        # presses ACCUMULATE (each press = +1 step, extending the in-flight move)
        # instead of each press re-reading the moving position and re-targeting a
        # closer point (which interrupted the move). Reset on any non-step motion
        # or stop for that axis. See relative_move / _clear_step_target.
        self._step_targets: dict[int, int] = {}
        self._manual_targets: dict[int, int] = {}
        self.is_open = False
        self._axis_calibrations = dict(AXIS_CALIBRATIONS)
        self._api_lock = threading.RLock()
        self._home_generation = 0
        self._home_owner: Axis9HomeJob | None = None
        if type(cutting_qualified) is not bool:
            raise ValueError("cutting_qualified must be an explicit bool")
        # Qualification is operator-supplied, never inferred from DLL exports or
        # the type of injected API. Batch 1 ships with no automatic qualification.
        self._cutting_qualified = cutting_qualified
        self._motion_revision = 0
        self._cutting_owner: object | None = None
        self._cutting_generation = 0
        self._cutting_state = "idle"
        self._cutting_reason: str | None = None
        self._cutting_target: tuple[int, int] | None = None
        self._cutting_cleanup_required = False
        self._cutting_crd_touched = False
        self._cutting_cancelled_owner: object | None = None
        self._cutting_stop_confirmed = False
        self._cutting_gate_output: CuttingGateOutput | None = None
        self._cutting_gate_qualified = False
        self._cutting_live_gate: CuttingGateOutput | None = None
        self._cutting_prepared: _PreparedCuttingLine | None = None
        # Retain identities: even plain object() tokens cannot be reused by a
        # later lease while an old queued callback still holds that token.
        self._cutting_used_owners: list[object] = []

    @property
    def motion_revision(self) -> int:
        with self._api_lock:
            return self._motion_revision

    @property
    def cutting_qualified(self) -> bool:
        """Operator attestation, default False; use set_cutting_qualified to change."""
        with self._api_lock:
            return self._cutting_qualified

    @property
    def qualified_cutting_gate(self) -> CuttingGateOutput | None:
        """Explicitly attested output for this open connection, otherwise None."""
        with self._api_lock:
            return (self._cutting_gate_output if self.is_open and self._cutting_qualified
                    and self._cutting_gate_qualified else None)

    @property
    def cutting_acceleration_um_s2(self) -> float:
        """Planner acceleration, not measured mechanics: a * 1e9 / pulse_per_mm."""
        with self._api_lock:
            scale = self._cutting_calibrations()[0].pulses_per_unit
            return self._finite(CUTTING_ACCELERATION_PULSE_MS2 * 1e9 / scale,
                                "cutting physical acceleration")

    @_manual_mutation
    def configure_cutting_gate(self, output: CuttingGateOutput, qualified: bool = False) -> None:
        """Record mapping only; no IO. True attests verified external/BufIO mapping.

        Attestation includes polarity, FIFO timing/continuity, readback and OFF
        behavior. Set motion qualification first. Reset, a new connection,
        calibration, or any set_cutting_qualified call revokes this attestation.
        """
        self._require_open()
        if not isinstance(output, CuttingGateOutput) or type(qualified) is not bool:
            raise MotionControllerError("explicit CuttingGateOutput and bool qualification required")
        if qualified and (not self._cutting_qualified or not self._gate_capable()):
            raise MotionControllerError("gate qualification requires qualified motion and gate DLL capability")
        self._cutting_gate_output = output
        self._cutting_gate_qualified = qualified

    def _gate_capable(self) -> bool:
        return (getattr(self.api, "supports_cutting_gate", True) is True
                and all(callable(getattr(self.api, name, None)) for name in
                        ("buf_io", "set_ext_do_bit", "get_ext_do_bit")))

    def _require_live_gate(self, output: CuttingGateOutput) -> None:
        if not isinstance(output, CuttingGateOutput) or self.qualified_cutting_gate != output:
            raise MotionControllerError("cutting gate output is not qualified/matching for this connection")
        if not self._coordinate_capable() or not self._gate_capable():
            raise MotionControllerError("coordinated gate DLL capability is unavailable")

    def _read_gate_off(self, output: CuttingGateOutput) -> None:
        try:
            value = self.api.get_ext_do_bit(output.card_index, output.bit_index)
        except Exception as exc:
            raise MotionControllerError(f"gate OFF readback failed: {exc}") from exc
        if type(value) is not int or value not in (0, 1) or value != output.off_level:
            raise MotionControllerError(f"gate OFF is not confirmed by digital output readback: {value!r}")

    def _write_gate_off(self, output: CuttingGateOutput) -> None:
        self._check(self.api.set_ext_do_bit(output.card_index, output.bit_index, output.off_level),
                    "GA_SetExtDoBit(OFF)")
        self._read_gate_off(output)

    @property
    def axis_calibrations(self):
        # Prevent dict aliasing/item edits from bypassing the lease and revision.
        with self._api_lock:
            return MappingProxyType(self._axis_calibrations)

    @axis_calibrations.setter
    def axis_calibrations(self, calibrations) -> None:
        self.set_axis_calibrations(calibrations)

    def _guard_cutting(self) -> None:
        if self._cutting_owner is not None or self._cutting_cleanup_required:
            raise MotionControllerError("cutting lease/recovery blocks manual motion and configuration")

    def _require_cutting_owner(self, owner: object) -> None:
        self._require_open()
        if owner is None or self._cutting_owner is not owner:
            raise MotionControllerError("cutting owner is stale, cancelled, or not acquired")

    @_api_locked
    def get_cutting_lease_status(self, owner: object) -> CuttingLeaseStatus:
        """No DLL reads; invalid owners must never launch/poll/release another run."""
        cancelled = owner is not None and self._cutting_cancelled_owner is owner
        return CuttingLeaseStatus(
            owner is not None and self._cutting_owner is owner and self.is_open,
            self._cutting_generation, self._cutting_state, self._cutting_reason,
            self._cutting_qualified,
            self._cutting_cleanup_required,
            cancelled and self._cutting_stop_confirmed,
            cancelled and self._cutting_cleanup_required and self._cutting_owner is None and self.is_open,
        )

    @_manual_mutation
    def set_cutting_qualified(self, qualified: bool) -> None:
        """Explicit commissioning attestation for this connection/configuration.

        True means an operator verified pack(2), profiles 5/6, absolute origin,
        CRD start/status/stop/FIFO semantics and configured speed/acceleration on
        the actual board. Export discovery and fake injection do NOT attest this.
        Reset, disconnect and calibration changes revoke qualification.
        """
        if type(qualified) is not bool:
            raise MotionControllerError("cutting qualification must be an explicit bool")
        self._cutting_qualified = qualified
        self._cutting_gate_qualified = False

    @staticmethod
    def _finite(value, label: str) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise MotionControllerError(f"{label} must be finite") from exc
        if not math.isfinite(result):
            raise MotionControllerError(f"{label} must be finite")
        return result

    def _cutting_calibrations(self):
        calibrations = []
        for axis in CUTTING_AXES:
            calibration = self.axis_calibrations.get(axis)
            if calibration is None or calibration.axis != axis or calibration.unit != "mm":
                raise MotionControllerError(f"axis {axis} requires a calibrated mm coordinate")
            for field in ("pulses_per_unit", "zero_pulse", "zero_coordinate",
                          "pulse_sign", "min_coordinate", "max_coordinate"):
                self._finite(getattr(calibration, field), f"axis {axis} {field}")
            if (calibration.pulses_per_unit <= 0 or abs(calibration.pulse_sign) != 1
                    or calibration.min_coordinate >= calibration.max_coordinate):
                raise MotionControllerError(f"axis {axis} has invalid cutting calibration")
            calibrations.append(calibration)
        # GA_LnXY's scalar vector speed is in pulse space. Unequal scales need a
        # separate geometric transform, deliberately outside Batch 1.
        if calibrations[0].pulses_per_unit != calibrations[1].pulses_per_unit:
            raise MotionControllerError("cutting requires equal X3/Y3 pulse/mm scales")
        return calibrations

    def _cutting_positions(self) -> dict[int, float]:
        return {axis: self._finite(self._get_prf_pos(axis), f"axis {axis} planner position")
                for axis in (5, 6, 7)}

    def _cutting_statuses(self, *, require_idle: bool) -> dict[int, int]:
        statuses = {}
        if not callable(getattr(self.api, "get_status", None)):
            raise MotionControllerError("cutting requires axis status feedback")
        for axis in ALL_AXES:
            try:
                status = self.api.get_status(axis)
            except RuntimeError as exc:
                raise MotionControllerError(str(exc)) from exc
            if not isinstance(status, int) or not 0 <= status <= 0x3FFFF:
                raise MotionControllerError(f"axis {axis} unknown status: {status!r}")
            statuses[axis] = status
            if (require_idle or axis not in CUTTING_AXES) and status & (
                    AXIS_STATUS_RUNNING | AXIS_STATUS_HOME_RUNNING):
                raise MotionControllerError(f"axis {axis} has competing motion/home")
            if axis in CUTTING_AXES and status & CUTTING_AXIS_FAULT_MASK:
                raise MotionControllerError(f"axis {axis} abnormal cutting status 0x{status:x}")
            if axis in CUTTING_AXES and status & AXIS_STATUS_HOME_RUNNING:
                raise MotionControllerError(f"axis {axis} has competing home")
            if require_idle and axis in self._manual_targets:
                position = self._finite(self._get_prf_pos(axis), "manual planner position")
                if abs(position - self._manual_targets[axis]) > CUTTING_POSITION_TOLERANCE_PULSE:
                    raise MotionControllerError(f"axis {axis} manual target is not confirmed complete")
                self._manual_targets.pop(axis)
        return statuses

    def _coordinate_capable(self) -> bool:
        return (getattr(self.api, "supports_coordinated_motion", True) is True
                and all(callable(getattr(self.api, name, None)) for name in
                        ("set_crd_prm", "line_xy", "crd_start", "crd_status", "crd_clear")))

    def _read_crd_status(self) -> tuple[int, int]:
        try:
            running, segment = self.api.crd_status(CUTTING_CRD, CUTTING_FIFO)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise MotionControllerError(f"cutting coordinate status failed: {exc}") from exc
        if running not in (0, 1) or not isinstance(segment, int) or segment < 0:
            raise MotionControllerError(f"unknown cutting coordinate status: {(running, segment)!r}")
        return running, segment

    @_api_locked
    def acquire_cutting(self, owner: object) -> None:
        """Reserve all motion for a DARK sample5/6 run; qualification not needed to capture."""
        self._require_open()
        self._guard_cutting()
        if owner is None or any(previous is owner for previous in self._cutting_used_owners):
            raise MotionControllerError("each cutting lease requires a fresh non-None owner")
        if self._home_owner is not None:
            raise MotionControllerError("homing owns the controller")
        self._cutting_calibrations()
        self._cutting_statuses(require_idle=True)
        if self._coordinate_capable() and self._read_crd_status()[0]:
            raise MotionControllerError("coordinate motion is already running")
        self._invalidate_home()  # queued but not-yet-started home jobs are stale
        self._cutting_owner = owner
        self._cutting_cancelled_owner = None
        self._cutting_stop_confirmed = False
        self._cutting_used_owners.append(owner)
        self._cutting_generation += 1
        self._cutting_state, self._cutting_reason = "idle", None
        self._cutting_target = None

    @_api_locked
    def get_cutting_reference(self) -> dict[int, float]:
        """Finite raw PLANNER pulses for axes1..9, serialized as one snapshot.

        This is not encoder feedback. Capture under a lease to exclude local
        mutations; hardware itself cannot supply an atomic nine-axis snapshot.
        """
        self._require_open()
        return {axis: self._finite(self._get_prf_pos(axis), f"axis {axis} planner position")
                for axis in ALL_AXES}

    @_api_locked
    def get_cutting_pose(self) -> tuple[float, float]:
        """X3/Y3 planned absolute coordinates in um, never physical encoders."""
        self._require_open()
        calibrations = self._cutting_calibrations()
        return tuple(self._finite(c.coordinate_from_pulse(
            self._finite(self._get_prf_pos(c.axis), "planner position")) * 1000.0,
            "cutting pose") for c in calibrations)

    def _cutting_target_pulses(self, target_um) -> tuple[int, int]:
        try:
            x, y = target_um
        except (TypeError, ValueError) as exc:
            raise MotionControllerError("cutting target must be exactly (X3, Y3) in um") from exc
        targets = []
        for c, value in zip(self._cutting_calibrations(), (x, y)):
            coordinate = self._finite(value, "cutting target um") / 1000.0
            if not c.min_coordinate <= coordinate <= c.max_coordinate:
                raise MotionControllerError(f"axis {c.axis} cutting target outside soft range")
            raw_pulse = self._finite(c.zero_pulse + (coordinate - c.zero_coordinate)
                                    * c.pulses_per_unit / c.pulse_sign, "target pulse")
            pulse = _round_half_up(raw_pulse)
            if not -(2 ** 31) <= pulse < 2 ** 31:
                raise MotionControllerError("cutting target exceeds int32 pulse range")
            targets.append(pulse)
        return tuple(targets)

    def _validate_cutting_segment(self, start, target, positions, statuses) -> None:
        # Rectangular soft ranges and the linear X2+X3 bound are convex: checking
        # BOTH endpoints proves a straight segment stays within these model bounds.
        for point in (start, target):
            for axis, pulse in zip(CUTTING_AXES, point):
                check = is_target_safe(axis, pulse, positions, self.axis_calibrations)
                if not check.ok:
                    raise MotionControllerError(check.message)
        for axis, old, new in zip(CUTTING_AXES, start, target):
            status = statuses[axis]
            if ((new > old and status & AXIS_STATUS_POS_HARD_LIMIT)
                    or (new < old and status & AXIS_STATUS_NEG_HARD_LIMIT)):
                raise MotionControllerError(f"axis {axis} cutting direction enters active hard limit")

    def validate_cutting_path(self, owner: object, targets_um) -> None:
        """Read-only preflight of ALL endpoints, including approach/preload.

        Caller must submit the complete route before the first move. Every actual
        start revalidates its segment against fresh planner/status feedback too.
        No FIFO queue/preload bypass is exposed in Batch 1.
        """
        with self._api_lock:
            self._require_cutting_owner(owner)
            if self._cutting_target is not None:
                raise MotionControllerError("cannot validate a path while cutting move is pending")
            self._cutting_calibrations()
            self._cutting_statuses(require_idle=True)
            positions = self._cutting_positions()
            start = (positions[5], positions[6])
            revision = self._motion_revision
        found = False
        try:
            targets = iter(targets_um)
        except TypeError as exc:
            raise MotionControllerError("cutting path must be an iterable of endpoints") from exc
        for target_um in targets:
            # Bound each locked transaction to one endpoint so a long path can
            # be cancelled without waiting for all its partner/status reads.
            with self._api_lock:
                self._require_cutting_owner(owner)
                if revision != self._motion_revision:
                    raise MotionControllerError("motion changed during cutting preflight")
                target = self._cutting_target_pulses(target_um)
                statuses = self._cutting_statuses(require_idle=True)
                positions[7] = self._finite(self._get_prf_pos(7), "X2 planner position")
                self._validate_cutting_segment(start, target, positions, statuses)
                start = target
                found = True
        with self._api_lock:
            self._require_cutting_owner(owner)
            if revision != self._motion_revision:
                raise MotionControllerError("motion changed during cutting preflight")
        if not found:
            raise MotionControllerError("cutting path must contain at least one endpoint")

    @_api_locked
    def start_cutting_move(self, owner: object, target_um: tuple[float, float],
                           speed_um_s: float) -> None:
        self._require_cutting_owner(owner)
        if self._cutting_prepared is not None:
            raise MotionControllerError("prepared cutting line must be started or cancelled first")
        if not self._cutting_qualified:
            raise MotionControllerError("coordinated cutting is not board-qualified; operator commissioning required")
        if not self._coordinate_capable():
            raise MotionControllerError("coordinated motion DLL capability is unavailable")
        if self._cutting_target is not None:
            raise MotionControllerError("previous cutting move has not been confirmed complete")
        speed = self._finite(speed_um_s, "cutting speed um/s")
        if not 0 < speed <= CUTTING_MAX_SPEED_UM_S:
            raise MotionControllerError(f"cutting speed must be >0 and <= {CUTTING_MAX_SPEED_UM_S:g} um/s")
        target = self._cutting_target_pulses(target_um)
        self.validate_cutting_path(owner, (target_um,))
        if self._read_crd_status()[0]:
            raise MotionControllerError("coordinate motion is already running")
        scale = self._cutting_calibrations()[0].pulses_per_unit
        velocity = self._finite(speed * scale / 1_000_000.0, "cutting vector velocity")
        if velocity <= 0:
            raise MotionControllerError("cutting vector velocity underflow")
        prm = TCrdPrm()
        prm.dimension = 2
        prm.profile[0], prm.profile[1] = CUTTING_AXES
        prm.synVelMax, prm.synAccMax = velocity, CUTTING_ACCELERATION_PULSE_MS2
        # Absolute raw planner pulses: explicit ZERO origin, never the current pose.
        prm.setOriginFlag = 1
        self._motion_revision += 1
        self._cutting_crd_touched = True  # partial config/enqueue failures need cleanup
        try:
            self._check(self.api.crd_clear(CUTTING_CRD, CUTTING_FIFO), "GA_CrdClear")
            self._check(self.api.set_crd_prm(CUTTING_CRD, prm), "GA_SetCrdPrm")
            self._check(self.api.line_xy(CUTTING_CRD, *target, velocity,
                        CUTTING_ACCELERATION_PULSE_MS2, 0.0, CUTTING_FIFO, 1), "GA_LnXY")
            # CRD mask is NOT the axis mask: start coordinate1 once for BOTH axes.
            self._check(self.api.crd_start(1 << (CUTTING_CRD - 1), 0), "GA_CrdStart")
            self._cutting_target = target
            self._cutting_state = "moving"
            for axis in CUTTING_AXES:
                self._clear_step_target(axis)
        except Exception as exc:
            self._cutting_fault(exc)

    @staticmethod
    def _ordered_cutting_line(points, tolerance: float) -> tuple[float, float]:
        """Check collinearity/order and return available acceleration/deceleration lengths."""
        dx, dy = (points[2][i] - points[1][i] for i in (0, 1))
        length = math.hypot(dx, dy)
        if not math.isfinite(length) or length <= 0:
            raise MotionControllerError("cutting line requires distinct finite start/end points")
        tangent = (dx / length, dy / length)
        projections = []
        for point in points:
            offset = (point[0] - points[1][0], point[1] - points[1][1])
            cross = offset[0] * tangent[1] - offset[1] * tangent[0]
            if abs(cross) > tolerance:
                raise MotionControllerError("lead-in, cut start/end and lead-out must be collinear")
            projections.append(offset[0] * tangent[0] + offset[1] * tangent[1])
        if any(b <= a for a, b in zip(projections, projections[1:])):
            raise MotionControllerError("cutting points must be ordered lead-in -> start -> end -> lead-out")
        return projections[1] - projections[0], projections[3] - projections[2]

    @_api_locked
    def prepare_cutting_line(self, owner: object, cut_start_um: tuple[float, float],
                             cut_end_um: tuple[float, float], lead_out_um: tuple[float, float],
                             speed_um_s: float, output: CuttingGateOutput) -> None:
        """Validate and queue one gated straight line WITHOUT starting it.

        Current planner pose is lead-in. Host must obtain Arduino FOLLOW readiness
        and maintain an independent inhibit before start_prepared_cutting_line.
        BufIO ordering/continuous boundary speed are commissioning requirements,
        not inferred from the presence of exports. No optical OFF claim is made.
        """
        self._require_cutting_owner(owner)
        self._require_live_gate(output)
        if self._cutting_prepared is not None or self._cutting_target is not None:
            raise MotionControllerError("another cutting move is prepared or pending")
        speed = self._finite(speed_um_s, "cutting speed um/s")
        if not 0 < speed <= CUTTING_MAX_SPEED_UM_S:
            raise MotionControllerError(f"cutting speed must be >0 and <= {CUTTING_MAX_SPEED_UM_S:g} um/s")
        # Convert shape and all endpoints before any gate write or FIFO mutation.
        targets = tuple(self._cutting_target_pulses(p) for p in
                        (cut_start_um, cut_end_um, lead_out_um))
        targets_um = tuple(tuple(self._finite(v, "cutting target um") for v in p)
                           for p in (cut_start_um, cut_end_um, lead_out_um))
        self.validate_cutting_path(owner, targets_um)
        if self._read_crd_status()[0]:
            raise MotionControllerError("coordinate motion is already running")
        reference = self.get_cutting_reference()
        lead_in = (reference[5], reference[6])
        calibrations = self._cutting_calibrations()
        scale = calibrations[0].pulses_per_unit
        pose = tuple(c.coordinate_from_pulse(reference[c.axis]) * 1000 for c in calibrations)
        # One diagonal pulse of tolerance accommodates endpoint quantization,
        # while both physical and actual pulse-space segments must stay ordered.
        tolerance = math.sqrt(2) * CUTTING_POSITION_TOLERANCE_PULSE
        self._ordered_cutting_line((pose, *targets_um), tolerance * 1000 / scale)
        available = self._ordered_cutting_line((lead_in, *targets), tolerance)
        velocity = self._finite(speed * scale / 1_000_000.0, "cutting vector velocity")
        required = self._finite(velocity * velocity / (2 * CUTTING_ACCELERATION_PULSE_MS2),
                                "cutting lead distance")
        if velocity <= 0 or min(available) + 1e-9 < required:
            raise MotionControllerError("lead-in/out too short for requested speed and planner acceleration")
        # Revalidate the actual reference endpoints too; reference reads follow
        # preflight and must not silently substitute an unsafe starting position.
        statuses = self._cutting_statuses(require_idle=True)
        previous = lead_in
        for target in targets:
            self._validate_cutting_segment(previous, target, reference, statuses)
            previous = target

        prm = TCrdPrm()
        prm.dimension, prm.profile[0], prm.profile[1] = 2, 5, 6
        prm.synVelMax, prm.synAccMax = velocity, CUTTING_ACCELERATION_PULSE_MS2
        prm.setOriginFlag = 1  # absolute raw planner pulses, zero-filled origins
        self._motion_revision += 1
        self._cutting_live_gate = output  # retain through partial write/enqueue failures
        self._cutting_crd_touched = True
        try:
            self._write_gate_off(output)
            self._check(self.api.crd_clear(CUTTING_CRD, CUTTING_FIFO), "GA_CrdClear")
            self._check(self.api.set_crd_prm(CUTTING_CRD, prm), "GA_SetCrdPrm")
            # Direct outputs are ONLY ever set OFF. ON exists solely in the CRD
            # FIFO between the accelerated lead-in and decelerated lead-out.
            for index, (target, level, end_velocity) in enumerate(zip(
                    targets, (output.off_level, 1 - output.off_level, output.off_level),
                    (velocity, velocity, 0.0))):
                self._check(self.api.buf_io(CUTTING_CRD, output.buf_do_type, output.card_index,
                            output.mask, output.mask if level else 0, CUTTING_FIFO, index * 2 + 1),
                            "GA_BufIO")
                self._check(self.api.line_xy(CUTTING_CRD, *target, velocity,
                            CUTTING_ACCELERATION_PULSE_MS2, end_velocity, CUTTING_FIFO, index * 2 + 2),
                            "GA_LnXY")
            self._cutting_prepared = _PreparedCuttingLine(
                output, targets_um, tuple(reference[axis] for axis in ALL_AXES), targets[-1])
            self._cutting_state = "prepared"
        except Exception as exc:
            self._cutting_fault(exc)

    @_api_locked
    def start_prepared_cutting_line(self, owner: object) -> None:
        """Start exactly once, after the host has confirmed FOLLOW readiness."""
        self._require_cutting_owner(owner)
        prepared = self._cutting_prepared
        if prepared is None:
            raise MotionControllerError("no prepared cutting line for this owner")
        try:
            self._require_live_gate(prepared.output)
            reference = self.get_cutting_reference()
            if any(abs(reference[axis] - old) > CUTTING_POSITION_TOLERANCE_PULSE
                   for axis, old in zip(ALL_AXES, prepared.reference)):
                raise MotionControllerError("stage reference changed after cutting preparation")
            self.validate_cutting_path(owner, prepared.targets_um)
            if self._read_crd_status()[0]:
                raise MotionControllerError("prepared coordinate unexpectedly running")
            self._read_gate_off(prepared.output)
            self._motion_revision += 1
            self._check(self.api.crd_start(1 << (CUTTING_CRD - 1), 0), "GA_CrdStart")
            self._cutting_target = prepared.final_target
            self._cutting_prepared = None
            self._cutting_state = "moving"
            for axis in CUTTING_AXES:
                self._clear_step_target(axis)
        except Exception as exc:
            self._cutting_fault(exc)

    def _invalidate_cutting(self, reason: str, state: str = "cancelled") -> None:
        self._cutting_owner = None
        self._cutting_target = None
        self._cutting_prepared = None
        self._cutting_generation += 1
        self._cutting_state, self._cutting_reason = state, reason

    def _stop_cutting_transaction(self, reason: str, mask: int = CUTTING_AXIS_MASK) -> None:
        """Try stop AND FIFO clear, retaining a motion interlock until confirmed.

        No wait/sleep under the lock. If the card is still decelerating, recovery
        remains blocked; the caller can retry stop_all after a later worker tick.
        """
        touched = self._cutting_crd_touched
        gate = self._cutting_live_gate or self.qualified_cutting_gate
        self._cutting_live_gate = gate  # retain the trusted mapping if OFF fails
        if self._cutting_owner is not None:
            self._cutting_cancelled_owner = self._cutting_owner
        self._cutting_stop_confirmed = False
        self._invalidate_cutting(reason)
        self._cutting_cleanup_required = True
        self._motion_revision += 1
        errors = []
        if gate is not None:
            try:
                self._write_gate_off(gate)
            except Exception as exc:
                errors.append(str(exc))
        try:
            self._check(self.api.stop(mask, 0), "GA_Stop(cutting cancellation)")
        except Exception as exc:
            errors.append(str(exc))
        if touched:
            try:
                self._check(self.api.crd_clear(CUTTING_CRD, CUTTING_FIFO), "GA_CrdClear")
            except Exception as exc:
                errors.append(str(exc))
        if gate is not None:
            # A queued ON may race the first OFF while GA_Stop decelerates.
            # Reassert/read OFF after clearing the FIFO as well.
            try:
                self._write_gate_off(gate)
            except Exception as exc:
                errors.append(str(exc))
        try:
            for axis in CUTTING_AXES:
                status = self.api.get_status(axis)
                if not isinstance(status, int) or not 0 <= status <= 0x3FFFF:
                    raise MotionControllerError("unknown stop confirmation status")
                if status & (AXIS_STATUS_RUNNING | AXIS_STATUS_HOME_RUNNING):
                    raise MotionControllerError("cutting stop is not yet confirmed idle")
            if touched and self._read_crd_status()[0]:
                raise MotionControllerError("cutting CRD stop is not yet confirmed idle")
        except Exception as exc:
            errors.append(str(exc))
        for axis in CUTTING_AXES:
            self._clear_step_target(axis)
        if errors:
            self._cutting_state = "fault"
            self._cutting_reason = reason + "; " + "; ".join(errors)
            raise MotionControllerError(self._cutting_reason)
        self._cutting_cleanup_required = False
        self._cutting_crd_touched = False
        self._cutting_live_gate = None
        self._cutting_stop_confirmed = True

    def _cutting_fault(self, fault: Exception) -> None:
        try:
            self._stop_cutting_transaction(str(fault))
        except MotionControllerError as cleanup:
            raise MotionControllerError(f"{fault}; cleanup: {cleanup}") from fault
        self._cutting_state = "fault"
        raise MotionControllerError(str(fault)) from fault

    @_api_locked
    def cutting_move_status(self, owner: object) -> bool:
        """True only for confirmed planner arrival; never an encoder assertion."""
        self._require_cutting_owner(owner)
        try:
            statuses = self._cutting_statuses(require_idle=False)
            positions = self._cutting_positions()
            self._validate_cutting_segment((positions[5], positions[6]),
                                          (positions[5], positions[6]), positions, statuses)
            running = self._read_crd_status()[0] if self._cutting_crd_touched else 0
            if self._cutting_prepared is not None:
                if running or any(statuses[a] & AXIS_STATUS_RUNNING for a in CUTTING_AXES):
                    raise MotionControllerError("prepared cutting line moved before explicit start")
                self._read_gate_off(self._cutting_prepared.output)
                return False
            if running or any(statuses[a] & AXIS_STATUS_RUNNING for a in CUTTING_AXES):
                return False
            if self._cutting_target is not None:
                if any(abs(positions[a] - target) > CUTTING_POSITION_TOLERANCE_PULSE
                       for a, target in zip(CUTTING_AXES, self._cutting_target)):
                    raise MotionControllerError("cutting stopped before planner target was reached")
                if self._cutting_live_gate is not None:
                    self._read_gate_off(self._cutting_live_gate)
                self._cutting_target = None
            elif self._cutting_live_gate is not None:
                self._read_gate_off(self._cutting_live_gate)
            self._cutting_state = "idle"
            return True
        except Exception as exc:
            self._cutting_fault(exc)

    @_api_locked
    def release_cutting(self, owner: object) -> None:
        self._require_cutting_owner(owner)
        if self._cutting_prepared is not None:
            raise MotionControllerError("cannot release a prepared cutting line; cancel or start it")
        if not self.cutting_move_status(owner):
            raise MotionControllerError("cannot release cutting lease while motion is running")
        if self._cutting_crd_touched:
            try:
                self._check(self.api.crd_clear(CUTTING_CRD, CUTTING_FIFO), "GA_CrdClear")
            except Exception as exc:
                self._cutting_fault(exc)
        self._cutting_crd_touched = False
        self._cutting_live_gate = None
        self._invalidate_cutting("cutting lease released", "released")

    @_api_locked
    def stop_cutting(self, owner: object) -> None:
        """Cancel, or retry this token's pending stop; never touch a newer lease.

        Check get_cutting_lease_status(owner).stop_confirmed after cancellation.
        A successful stop invalidates the lease, so do not release it afterwards.
        """
        self._require_open()
        retry = (owner is not None and self._cutting_cancelled_owner is owner
                 and self._cutting_owner is None and self._cutting_cleanup_required)
        if not retry:
            self._require_cutting_owner(owner)
        self._stop_cutting_transaction("cutting cancelled")

    def _check(self, result: int, action: str) -> None:
        if result != 0:
            _log.error("motion API call failed: %s returned %s", action, result)
            raise MotionControllerError(f"{action} failed with {result}")

    def _get_prf_pos(self, axis: int) -> float:
        """Call api.get_prf_pos and translate RuntimeError → MotionControllerError.

        gas_api is a thin DLL wrapper that raises RuntimeError on nonzero DLL return.
        All GUI-reachable read paths must surface a consistent MotionControllerError
        so the error is handled by the existing (MotionControllerError, ValueError)
        handlers in the board-action and async refresh paths.
        """
        try:
            return self.api.get_prf_pos(axis)
        except RuntimeError as exc:
            raise MotionControllerError(str(exc)) from exc

    def _require_open(self) -> None:
        if not self.is_open:
            raise MotionControllerError("board is not open")

    @_manual_mutation
    def set_axis_calibrations(self, axis_calibrations) -> None:
        self._axis_calibrations = dict(axis_calibrations or AXIS_CALIBRATIONS)
        self._cutting_qualified = False
        self._cutting_gate_qualified = False

    @_api_locked
    def open_board(self) -> None:
        self._guard_cutting()
        self._cutting_gate_qualified = False
        if self.is_open:
            # Guard against a second open re-issuing GA_Open and re-enabling all
            # axes on an already-live connection.
            return
        # Validate IP address at the controller boundary before any DLL call so
        # the error surfaces as MotionControllerError regardless of which api
        # implementation is in use (real GasApi or a test double).
        if not self.board_ip:
            raise MotionControllerError("board IP address must not be empty")
        if "\x00" in self.board_ip:
            raise MotionControllerError("board IP address must not contain NUL characters")
        try:
            self.board_ip.encode("ascii")
        except UnicodeEncodeError as exc:
            raise MotionControllerError(f"board IP address is not valid ASCII: {self.board_ip!r}") from exc

        self._motion_revision += 1
        opened = False
        try:
            try:
                rc = self.api.open_network(self.board_ip, self.open_mode)
            except ValueError as exc:
                raise MotionControllerError(f"invalid board IP address: {exc}") from exc
            self._check(rc, "GA_Open")
            opened = True
            # NOTE: do NOT call GA_SetCardNo here. For this single-board network
            # connection the DLL already defaults to card 0, and on the real
            # hardware GA_SetCardNo(0) returns error 7, which would abort the open.
            # (A prior "documents intent" addition broke opening the board.)

            self._configure_axes_and_limits()
        except Exception:
            if opened:
                self._best_effort_close_after_failed_open()
            self.is_open = False
            self._cutting_qualified = False
            raise

        self.is_open = True

    def _configure_axes_and_limits(self) -> None:
        """Enable axes and (re)apply the hard-limit configuration.

        Shared by open_board and reset_board so a reset restores the same
        axis-enable + hard-limit-sensor + GA_LmtsOn state as a fresh open
        (CP4). Must be called with the api lock held.
        """
        for axis in ALL_AXES:
            self._check(self.api.axis_on(axis), f"GA_AxisOn({axis})")
            self._check(self.api.clear_status(axis, 1), f"GA_ClrSts({axis})")
            self._check(self.api.limits_off(axis, -1), f"GA_LmtsOff({axis})")

        for axis, limits in AXIS_HARD_LIMITS.items():
            if not limits.has_any_limit:
                continue
            if limits.positive_io is not None:
                self._check(
                    self.api.set_hard_limit_positive(axis, 1, 0, limits.positive_io),
                    f"GA_SetHardLimP({axis})",
                )
            if limits.negative_io is not None:
                self._check(
                    self.api.set_hard_limit_negative(axis, 1, 0, limits.negative_io),
                    f"GA_SetHardLimN({axis})",
                )
            # GA_SetLmtSnsSingle: header-undeclared; see gas_api.py for inferred prototype note.
            self._check(self.api.set_limit_sense_single(axis, 0, 0), f"GA_SetLmtSnsSingle({axis})")
            if limits.positive_io is not None:
                self._check(self.api.limits_on(axis, LIMIT_TYPE_POSITIVE), f"GA_LmtsOn({axis}, positive)")
            if limits.negative_io is not None:
                self._check(self.api.limits_on(axis, LIMIT_TYPE_NEGATIVE), f"GA_LmtsOn({axis}, negative)")

        for axis in AXES_WITHOUT_LIMITS:
            self._check(self.api.limits_off(axis, -1), f"GA_LmtsOff({axis})")
            self._check(self.api.clear_status(axis, 1), f"GA_ClrSts({axis})")

    def _best_effort_close_after_failed_open(self) -> None:
        try:
            self.api.stop(0xFFFF, 0)
        except Exception:
            pass
        try:
            self.api.close()
        except Exception:
            pass

    @_api_locked
    def close_board(self) -> None:
        self._invalidate_home()
        self._cutting_live_gate = self._cutting_live_gate or self.qualified_cutting_gate
        self._cutting_gate_qualified = False
        self._cutting_qualified = False
        if not self.is_open:
            self._invalidate_cutting("board disconnected")
            return
        self._motion_revision += 1
        self._step_targets.clear()
        stop_error: Exception | None = None
        try:
            if (self._cutting_owner is not None or self._cutting_cleanup_required
                    or self._cutting_live_gate is not None):
                self._stop_cutting_transaction("board disconnected", 0xFFFF)
            else:
                self._invalidate_cutting("board disconnected")
                self._check(self.api.stop(0xFFFF, 0), "GA_Stop(all)")
        except Exception as exc:
            # A failing stop is exactly when we most need to still close and
            # de-energize the board. Remember the error but do not abort the close.
            stop_error = exc
        # GA_Close must always run. If it raises, is_open stays True so the close
        # can be retried; if it succeeds, mark the board closed.
        self._check(self.api.close(), "GA_Close")
        self.is_open = False
        self._manual_targets.clear()
        # A failed FIFO cleanup cannot be forgotten merely because the network
        # handle closed. Reopening must not permit old coordinate data to run.
        if stop_error is not None:
            raise MotionControllerError(str(stop_error)) from stop_error

    @_api_locked
    def reset_board(self) -> None:
        if self._cutting_owner is not None:
            raise MotionControllerError("reset rejected while cutting lease is active; stop first")
        self._invalidate_home()
        self._require_open()
        gate = self._cutting_live_gate or self.qualified_cutting_gate
        self._cutting_gate_qualified = False
        self._cutting_live_gate = gate
        # Recovery may reset only after a combined stop/FIFO cleanup is confirmed.
        if self._cutting_cleanup_required or gate is not None:
            self._stop_cutting_transaction("reset recovery")
        self._motion_revision += 1
        self._invalidate_cutting("board reset")
        self._cutting_qualified = False
        self._step_targets.clear()
        # Even a failed reset may change the physical output level. Retain the
        # old attested address for OFF recovery; its execution qualification is
        # already revoked and must never be restored implicitly.
        self._cutting_live_gate = gate
        try:
            self._check(self.api.reset(), "GA_Reset")
            if gate is not None:
                self._write_gate_off(gate)
        except Exception as exc:
            if gate is not None:
                self._cutting_fault(exc)
            raise
        self._cutting_live_gate = None
        self._manual_targets.clear()
        # CP4: GA_Reset may clear the controller's axis-enable / hard-limit-enable
        # state, so re-apply the SAME axis + hard-limit configuration that open_board
        # installs. Without this, after a reset the board stays is_open=True and can
        # be commanded to move, but axes driving into a physical hard limit would no
        # longer auto-stop until the next open. (Whether GA_Reset actually disables
        # GA_LmtsOn is unconfirmed in the vendor header/this repo — re-applying is
        # idempotent and safe either way; confirm exact GA_Reset register semantics
        # against the SDK / on real hardware. See handoff.md.)
        self._configure_axes_and_limits()

    @_manual_mutation
    def relative_move(self, axis: int, direction: int, speed: float, step: float) -> None:
        self._guard_axis9_owner(axis)
        self._require_open()
        axis = int(axis)
        direction = 1 if direction > 0 else -1
        self._assert_hard_limit_direction_allowed(axis, direction)
        current_position = self._get_prf_pos(axis)
        # ACCUMULATE rapid same-direction step presses: if a prior step's target is
        # still ahead of the axis in THIS direction (i.e. the move hasn't finished and
        # we're not reversing), step from that committed target so each press adds one
        # full step (extending the in-flight move) instead of re-targeting a closer
        # point from the moving position. Otherwise (fresh / completed / reversing)
        # step from the current position.
        prev_target = self._step_targets.get(axis)
        base = current_position
        if prev_target is not None and (
            (direction > 0 and prev_target > current_position)
            or (direction < 0 and prev_target < current_position)
        ):
            base = prev_target
        target_position = _round_half_up(base + (step * direction))
        if axis == 3:
            # Axis 3 has no soft-range validation (_validate_target is a no-op for it),
            # so apply the same int32 + sanity cap the other axis-3 target paths enforce.
            self._assert_microscope_z_target_in_bounds(target_position)
        self._validate_target(axis, target_position)
        # Only commit the accumulated target AFTER validation passes, so a refused
        # (out-of-range) step never advances the baseline for the next press.
        self._step_targets[axis] = target_position
        self._start_trap_move(axis, target_position, abs(speed), acc=0.1, dec=0.1)

    def _clear_step_target(self, axis: int) -> None:
        # Any non-step motion or stop on an axis ends its step-accumulation session,
        # so the next relative step restarts from the live position.
        self._step_targets.pop(int(axis), None)

    @_manual_mutation
    def move_axis_to_coordinate(self, axis: int, coordinate: float, speed: float | None = None) -> None:
        self._guard_axis9_owner(axis)
        self._require_open()
        axis = int(axis)
        self._clear_step_target(axis)  # an absolute move ends step accumulation on this axis
        if not axis_has_absolute_calibration(axis, self.axis_calibrations):
            raise MotionControllerError(
                f"axis {axis} has no absolute coordinate calibration; cannot move to a coordinate"
            )
        current_position = self._get_prf_pos(axis)
        target_position = pulse_from_coordinate(axis, float(coordinate), self.axis_calibrations)
        delta = target_position - current_position
        if delta != 0:
            self._assert_hard_limit_direction_allowed(axis, 1 if delta > 0 else -1)
        self._validate_target(axis, target_position)
        if axis == AXIS9_NUM and speed is None:
            velocity = AXIS9_MAX_VELOCITY
        elif speed is None:
            velocity = 20.0
        else:
            # Use the speed as given (do not treat an explicit 0 as "use 20").
            velocity = abs(float(speed))
        self._start_trap_move(axis, target_position, velocity, acc=0.1, dec=0.1)

    @_manual_mutation
    def start_jog(self, axis: int, speed: float, partner_lookahead_pulse: float = 0.0) -> None:
        self._guard_axis9_owner(axis)
        self._require_open()
        self._clear_step_target(axis)  # a continuous jog ends step accumulation on this axis
        direction = 1 if speed > 0 else -1
        if speed != 0:
            self._assert_hard_limit_direction_allowed(axis, direction)
            current_positions = self._current_positions_for_safety(axis)
            bounded_target = safe_continuous_target(
                axis, direction, current_positions, self.axis_calibrations, partner_lookahead_pulse
            )
            if bounded_target is not None:
                self._validate_target(axis, bounded_target)
                if int(round(current_positions[axis])) == int(bounded_target):
                    raise MotionControllerError(f"axis {axis} has no safe travel remaining in this direction")
                self._start_trap_move(axis, bounded_target, abs(speed), acc=0.5, dec=0.5)
                return

            # A bounded axis (one with a finite soft range) that returns no safe
            # continuous target means the requested direction would cross a soft
            # limit or the X2/X3 interference boundary. Refuse explicitly instead
            # of falling back to a velocity jog, which would discard the software
            # protection and rely only on the reactive watchdog.
            if axis_has_finite_soft_range(axis, self.axis_calibrations):
                raise MotionControllerError(tr("拒绝移动：该方向无安全行程 (axis {axis})").format(axis=axis))

            # Axis 3 (MicroscopeZ) has no finite soft range. It used to drive a
            # GA_PrfJog velocity jog here, but on the real card that mode did not
            # produce motion while the button/trigger was held (the axis appeared
            # to move only on release). Every other axis the operator drives uses a
            # GA_PrfTrap profiled move and behaves correctly, so axis 3 now does the
            # same: a trap move toward a far target at the MicroscopeZ sanity cap in
            # the jog direction. The operator releases (which issues stop_axis) long
            # before the cap is reached; the cap (±1000 mm-equivalent, well inside
            # int32) is only a backstop so a stuck button can never command an
            # absurd / int32-wrapping pulse. Velocity is abs(speed), so the gamepad
            # trigger's proportional fraction (up to the MicroscopeZ 4000 um/s =
            # 4 mm/s speed cap at full deflection) maps straight onto trap speed.
            # The cap is 4000 (not 5000) because the focus stepper STALLS at 5000
            # um/s = 250 pulse/ms; see MICROSCOPE_Z_SPEED_OPTIONS_UM_S.
            far_target = float(direction) * MICROSCOPE_Z_MAX_ABS_PULSE
            self._validate_target(axis, far_target)
            if int(round(current_positions[axis])) == int(far_target):
                raise MotionControllerError(f"axis {axis} has no safe travel remaining in this direction")
            self._start_trap_move(axis, far_target, abs(speed), acc=0.5, dec=0.5)
            return

        # speed == 0: a zero-velocity "jog" carries no motion. Real stops go through
        # stop_axis(); this legacy velocity-jog path remains only as a defensive
        # no-op fallthrough for an explicit zero-speed jog request.
        self._check(self.api.axis_on(axis), f"GA_AxisOn({axis})")
        self._check(self.api.profile_jog(axis), f"GA_PrfJog({axis})")
        self._check(self.api.clear_status(axis, 1), f"GA_ClrSts({axis})")
        self._check(self.api.set_jog_prm_single(axis, 0.5, 0.5, 0.0), f"GA_SetJogPrmSingle({axis})")
        self._check(self.api.set_velocity(axis, speed), f"GA_SetVel({axis})")
        self._check(self.api.update(axis_mask(axis)), f"GA_Update({axis})")

    @_api_locked
    def stop_axis(self, axis: int) -> None:
        if axis == AXIS9_NUM:
            self._invalidate_home()
        self._require_open()
        if (self._cutting_owner is not None or self._cutting_cleanup_required
                or self._cutting_live_gate is not None or self.qualified_cutting_gate is not None):
            self._stop_cutting_transaction("axis stop cancelled cutting", CUTTING_AXIS_MASK | axis_mask(axis))
            return
        self._motion_revision += 1
        self._clear_step_target(axis)
        self._check(self.api.stop(axis_mask(axis), 0), f"GA_Stop({axis})")
        self._manual_targets.pop(axis, None)

    @_api_locked
    def stop_all(self) -> None:
        self._invalidate_home()
        self._require_open()
        self._step_targets.clear()
        if (self._cutting_owner is not None or self._cutting_cleanup_required
                or self._cutting_live_gate is not None or self.qualified_cutting_gate is not None):
            self._stop_cutting_transaction("global stop cancelled cutting", 0xFFFF)
            return
        self._motion_revision += 1
        self._check(self.api.stop(0xFFFF, 0), "GA_Stop(all)")
        self._manual_targets.clear()

    @_api_locked
    def get_axis_position(self, axis: int) -> float:
        self._require_open()
        return self._get_prf_pos(axis)

    @_manual_mutation
    def zero_axis3(self) -> None:
        self._require_open()
        self._check(self.api.zero_position(3, 1), "GA_ZeroPos(3)")

    @_manual_mutation
    def mark_axis3(self) -> float:
        self._require_open()
        self.axis3_marked_position = self._get_prf_pos(3)
        return self.axis3_marked_position

    @_api_locked
    def move_axis3_to_mark(self) -> None:
        self.move_axis3_to_position(self.axis3_marked_position)

    def _assert_microscope_z_target_in_bounds(self, target: int) -> None:
        # GA_SetPos is bound to c_long (int32) and SILENTLY wraps mod 2^32 on overflow
        # (e.g. int(3e9) -> -1294967296, direction reversed), which could command a
        # wrong/negative MicroscopeZ position near the optics. Plus a conservative
        # physical sanity cap (axis 3 has no documented hardware travel — rejects only
        # corrupted/absurd targets, never a real reading). Shared by EVERY axis-3 trap
        # target path (point move + relative jog) so the guard cannot drift between them.
        target = int(target)
        if not (-(2 ** 31) <= target <= 2 ** 31 - 1):
            raise MotionControllerError(tr("MicroscopeZ 目标脉冲超出 int32 范围，已拒绝"))
        if abs(target) > MICROSCOPE_Z_MAX_ABS_PULSE:
            raise MotionControllerError(
                tr("MicroscopeZ 目标脉冲 {target} 超出合理范围 (±{bound})，已拒绝").format(target=target, bound=int(MICROSCOPE_Z_MAX_ABS_PULSE))
            )

    @_manual_mutation
    def move_axis3_to_position(self, position: float) -> None:
        self._require_open()
        self._clear_step_target(3)  # a point move ends step accumulation on MicroscopeZ
        axis = 3
        target = int(position)
        self._assert_microscope_z_target_in_bounds(target)
        self._check(self.api.profile_trap(axis), "GA_PrfTrap(3)")
        self._check(self.api.axis_on(axis), "GA_AxisOn(3)")
        self._check(self.api.clear_status(axis, 1), "GA_ClrSts(3)")
        self._check(self.api.set_trap_prm_single(axis, 1.0, 1.0, 0.0, 0), "GA_SetTrapPrmSingle(3)")
        self._check(self.api.set_velocity(axis, 20.0), "GA_SetVel(3)")
        self._check(self.api.set_position(axis, target), "GA_SetPos(3)")
        self._manual_targets[axis] = target
        self._check(self.api.update(axis_mask(axis)), "GA_Update(3)")

    @_api_locked
    def new_axis9_home_job(self) -> Axis9HomeJob:
        self._guard_cutting()
        return Axis9HomeJob(self, self._home_generation)

    def home_axis9(self) -> None:
        job = self.new_axis9_home_job()
        while job.poll() is None:
            time.sleep(0.02)

    def _invalidate_home(self) -> None:
        self._home_generation += 1
        self._home_owner = None

    def _guard_axis9_owner(self, axis: int) -> None:
        if axis == AXIS9_NUM and self._home_owner is not None:
            raise MotionControllerError(tr("R 轴正在回零"))

    @_api_locked
    def _poll_axis9_home(self, job: Axis9HomeJob) -> float | None:
        self._guard_cutting()
        self._require_open()
        if job.generation != self._home_generation:
            raise MotionControllerError(tr("R 轴回零已取消"))
        if self._home_owner is not None and self._home_owner is not job:
            raise MotionControllerError(tr("R 轴正在回零"))
        try:
            if job.phase == "start":
                angle = self.get_axis9_angle()
                self._assert_axis9_home_start_in_global_range(angle)
                if self._axis9_zero_switch_active():
                    return self._get_prf_pos(AXIS9_NUM)
                self._home_owner = job
                job.phase = "preposition"
                self._move_axis9_angle_locked(AXIS9_HOME_SEARCH_MIN_DEGREES,
                    self._axis9_home_velocity(AXIS9_HOME_PREPOSITION_SPEED_DEG_S))
                job.deadline = (time.monotonic() + AXIS9_HOME_PREPOSITION_TIMEOUT_MARGIN_S
                    + abs(angle - AXIS9_HOME_SEARCH_MIN_DEGREES) / AXIS9_HOME_PREPOSITION_SPEED_DEG_S)
                return None

            if job.phase == "scan" and self._axis9_zero_switch_active():
                self._motion_revision += 1
                self._check(self.api.stop(axis_mask(AXIS9_NUM), 0), "GA_Stop(9)")
                self._manual_targets.pop(AXIS9_NUM, None)
                job.phase = "stopping"
                job.deadline = time.monotonic() + AXIS9_HOME_IO_SEARCH_TIMEOUT_MARGIN_S

            status = self.api.get_status(AXIS9_NUM)
            arrived = status & AXIS_STATUS_ARRIVE and not status & AXIS_STATUS_RUNNING
            if (job.phase == "stopping" and not status & AXIS_STATUS_RUNNING) or arrived:
                if job.phase == "stopping" or self._axis9_zero_switch_active():
                    pulse = self._get_prf_pos(AXIS9_NUM)
                    self._manual_targets.pop(AXIS9_NUM, None)
                    self._home_owner = None
                    return pulse
                if job.phase == "preposition":
                    job.phase = "scan"
                    self._move_axis9_angle_locked(AXIS9_HOME_SEARCH_MAX_DEGREES,
                        self._axis9_home_velocity(AXIS9_HOME_IO_SEARCH_SPEED_DEG_S))
                    job.deadline = (time.monotonic() + AXIS9_HOME_IO_SEARCH_TIMEOUT_MARGIN_S
                        + (AXIS9_HOME_SEARCH_MAX_DEGREES - AXIS9_HOME_SEARCH_MIN_DEGREES)
                        / AXIS9_HOME_IO_SEARCH_SPEED_DEG_S)
                    return None
                raise MotionControllerError(tr("R 轴回零未完成：在 -10..10 deg 范围内未检测到 card0 IO8 零位开关，当前位置未设为 0 deg"))
            if time.monotonic() >= job.deadline:
                raise MotionControllerError(tr("R 轴回零等待超时"))
            return None
        except Exception as fault:
            if self._home_owner is job:
                self._invalidate_home()
                try:
                    self._motion_revision += 1
                    self._check(self.api.stop(axis_mask(AXIS9_NUM), 0), "GA_Stop(9)")
                    self._manual_targets.pop(AXIS9_NUM, None)
                except Exception as stop_fault:
                    raise MotionControllerError(f"{fault}; GA_Stop(9): {stop_fault}") from fault
            raise

    def _assert_axis9_home_start_in_global_range(self, current_angle: float) -> None:
        if current_angle < AXIS9_MIN_DEGREES or current_angle > AXIS9_MAX_DEGREES:
            raise MotionControllerError(
                tr(
                    "R 轴回零被拒绝：当前角度 {angle:.3f} deg 超出 R 轴安全范围 {min_angle:g}..{max_angle:g} deg"
                ).format(
                    angle=float(current_angle),
                    min_angle=AXIS9_MIN_DEGREES,
                    max_angle=AXIS9_MAX_DEGREES,
                )
            )

    def _axis9_home_velocity(self, degrees_per_second: float) -> float:
        return float(degrees_per_second) / AXIS9_DEGREES_PER_PULSE / 1000.0

    def _axis9_zero_switch_active(self) -> bool:
        value = self.api.get_ext_di_bit(AXIS9_ZERO_SWITCH_CARD_INDEX, AXIS9_ZERO_SWITCH_IO_INDEX)
        return int(value) == AXIS9_ZERO_SWITCH_ACTIVE_VALUE

    @_manual_mutation
    def move_axis9_to_angle(self, angle: float, speed: float | None = None) -> None:
        self._guard_axis9_owner(AXIS9_NUM)
        self._move_axis9_angle_locked(angle, speed)

    def _move_axis9_angle_locked(self, angle: float, speed: float | None) -> None:
        self._require_open()
        if angle < AXIS9_MIN_DEGREES or angle > AXIS9_MAX_DEGREES:
            raise MotionControllerError(
                f"axis 9 angle must be between {AXIS9_MIN_DEGREES:g} and {AXIS9_MAX_DEGREES:g}"
            )
        axis = AXIS9_NUM
        calibration = self.axis_calibrations.get(axis) or AXIS_CALIBRATIONS[axis]
        if calibration.min_coordinate is not None and float(angle) == float(calibration.min_coordinate):
            target_pulse = calibration.soft_limit_endpoint_pulse(float(angle))
        elif calibration.max_coordinate is not None and float(angle) == float(calibration.max_coordinate):
            target_pulse = calibration.soft_limit_endpoint_pulse(float(angle))
        else:
            target_pulse = pulse_from_coordinate(axis, angle, self.axis_calibrations)
        self._validate_target(axis, target_pulse)
        self._motion_revision += 1
        self._check(self.api.profile_trap(axis), "GA_PrfTrap(9)")
        self._check(self.api.axis_on(axis), "GA_AxisOn(9)")
        self._check(self.api.clear_status(axis, 1), "GA_ClrSts(9)")
        self._check(self.api.set_trap_prm_single(axis, 1.0, 1.0, 0.0, 0), "GA_SetTrapPrmSingle(9)")
        velocity = AXIS9_MAX_VELOCITY if speed is None else abs(float(speed))
        self._check(self.api.set_velocity(axis, velocity), "GA_SetVel(9)")
        self._check(self.api.set_position(axis, target_pulse), "GA_SetPos(9)")
        self._manual_targets[axis] = target_pulse
        self._check(self.api.update(axis_mask(axis)), "GA_Update(9)")

    @_api_locked
    def get_axis9_angle(self) -> float:
        self._require_open()
        coordinate = coordinate_from_pulse(AXIS9_NUM, self._get_prf_pos(AXIS9_NUM), self.axis_calibrations)
        if coordinate is None:
            return self._get_prf_pos(AXIS9_NUM) * self.axis9_degrees_per_pulse
        return float(coordinate)

    def _validate_target(self, axis: int, target_position: float) -> None:
        positions: dict[int, float] = {}
        if axis == 5:
            positions[7] = self._get_prf_pos(7)
        elif axis == 7:
            positions[5] = self._get_prf_pos(5)

        check = is_target_safe(axis, target_position, positions, self.axis_calibrations)
        if not check.ok:
            raise MotionControllerError(check.message)

    def _current_positions_for_safety(self, axis: int) -> dict[int, float]:
        positions = {int(axis): self._get_prf_pos(axis)}
        if axis == 5:
            positions[7] = self._get_prf_pos(7)
        elif axis == 7:
            positions[5] = self._get_prf_pos(5)
        return positions

    def _start_trap_move(self, axis: int, target_position: int, velocity: float, acc: float, dec: float) -> None:
        self._check(self.api.profile_trap(axis), f"GA_PrfTrap({axis})")
        self._check(self.api.axis_on(axis), f"GA_AxisOn({axis})")
        self._check(self.api.clear_status(axis, 1), f"GA_ClrSts({axis})")
        self._check(self.api.set_trap_prm_single(axis, acc, dec, 0.0, 0), f"GA_SetTrapPrmSingle({axis})")
        self._check(self.api.set_velocity(axis, abs(velocity)), f"GA_SetVel({axis})")
        self._check(self.api.set_position(axis, int(target_position)), f"GA_SetPos({axis})")
        self._manual_targets[axis] = int(target_position)
        self._check(self.api.update(axis_mask(axis)), f"GA_Update({axis})")

    def _assert_hard_limit_direction_allowed(self, axis: int, direction: int) -> None:
        limits = AXIS_HARD_LIMITS.get(int(axis))
        if limits is None or not limits.has_any_limit:
            return
        try:
            status = int(self.api.get_status(axis))
        except RuntimeError as exc:
            raise MotionControllerError(str(exc)) from exc

        if direction > 0 and limits.positive_io is not None and status & AXIS_STATUS_POS_HARD_LIMIT:
            raise MotionControllerError(f"axis {axis} positive limit is active")
        if direction < 0 and limits.negative_io is not None and status & AXIS_STATUS_NEG_HARD_LIMIT:
            raise MotionControllerError(f"axis {axis} negative limit is active")
