from __future__ import annotations

from functools import wraps
import logging
import threading

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
from .gas_api import AXIS_STATUS_NEG_HARD_LIMIT, AXIS_STATUS_POS_HARD_LIMIT, GasApi


_log = logging.getLogger("ats.motion")
_audit = logging.getLogger("ats.audit")


def _api_locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._api_lock:
            return method(self, *args, **kwargs)

    return wrapper


class GasMotionController:
    def __init__(
        self,
        api: GasApi,
        board_ip: str = BOARD_IP,
        open_mode: int = OPEN_MODE,
        axis9_degrees_per_pulse: float = AXIS9_DEGREES_PER_PULSE,
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
        self.is_open = False
        self.axis_calibrations = dict(AXIS_CALIBRATIONS)
        self._api_lock = threading.RLock()

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

    @_api_locked
    def set_axis_calibrations(self, axis_calibrations) -> None:
        self.axis_calibrations = axis_calibrations or AXIS_CALIBRATIONS

    @_api_locked
    def open_board(self) -> None:
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
        if not self.is_open:
            return
        self._step_targets.clear()
        stop_error: MotionControllerError | None = None
        try:
            self._check(self.api.stop(0xFFFF, 0), "GA_Stop(all)")
        except MotionControllerError as exc:
            # A failing stop is exactly when we most need to still close and
            # de-energize the board. Remember the error but do not abort the close.
            stop_error = exc
        # GA_Close must always run. If it raises, is_open stays True so the close
        # can be retried; if it succeeds, mark the board closed.
        self._check(self.api.close(), "GA_Close")
        self.is_open = False
        if stop_error is not None:
            raise stop_error

    @_api_locked
    def reset_board(self) -> None:
        self._require_open()
        self._step_targets.clear()
        self._check(self.api.reset(), "GA_Reset")
        # CP4: GA_Reset may clear the controller's axis-enable / hard-limit-enable
        # state, so re-apply the SAME axis + hard-limit configuration that open_board
        # installs. Without this, after a reset the board stays is_open=True and can
        # be commanded to move, but axes driving into a physical hard limit would no
        # longer auto-stop until the next open. (Whether GA_Reset actually disables
        # GA_LmtsOn is unconfirmed in the vendor header/this repo — re-applying is
        # idempotent and safe either way; confirm exact GA_Reset register semantics
        # against the SDK / on real hardware. See handoff.md.)
        self._configure_axes_and_limits()

    @_api_locked
    def relative_move(self, axis: int, direction: int, speed: float, step: float) -> None:
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

    @_api_locked
    def move_axis_to_coordinate(self, axis: int, coordinate: float, speed: float | None = None) -> None:
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

    @_api_locked
    def start_jog(self, axis: int, speed: float, partner_lookahead_pulse: float = 0.0) -> None:
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
        self._require_open()
        self._clear_step_target(axis)
        self._check(self.api.stop(axis_mask(axis), 0), f"GA_Stop({axis})")

    @_api_locked
    def stop_all(self) -> None:
        self._require_open()
        self._step_targets.clear()
        self._check(self.api.stop(0xFFFF, 0), "GA_Stop(all)")

    @_api_locked
    def get_axis_position(self, axis: int) -> float:
        self._require_open()
        return self._get_prf_pos(axis)

    @_api_locked
    def zero_axis3(self) -> None:
        self._require_open()
        self._check(self.api.zero_position(3, 1), "GA_ZeroPos(3)")

    @_api_locked
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

    @_api_locked
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
        self._check(self.api.update(axis_mask(axis)), "GA_Update(3)")

    @_api_locked
    def home_axis9(self) -> None:
        self._require_open()
        axis = AXIS9_NUM
        # The current GAS.dll is unstable when GA_HomeSetPrmSingle is called
        # directly from ctypes; the struct entrypoint matches the original C# path.
        self._check(
            self.api.home_set_prm(
                axis,
                home_mode=1,
                home_direction=1,
                offset=0,
                rapid_velocity=10.0,
                locate_velocity=2.0,
                index_velocity=1.0,
                acceleration=0.5,
                delay_before_zero=3000,
                home_max_distance=0,
            ),
            "GA_HomeSetPrm(9)",
        )
        self._check(self.api.home_start(axis), "GA_HomeStart(9)")

    @_api_locked
    def move_axis9_to_angle(self, angle: float, speed: float | None = None) -> None:
        self._require_open()
        if angle < AXIS9_MIN_DEGREES or angle > AXIS9_MAX_DEGREES:
            raise MotionControllerError(
                f"axis 9 angle must be between {AXIS9_MIN_DEGREES:g} and {AXIS9_MAX_DEGREES:g}"
            )
        axis = AXIS9_NUM
        target_pulse = pulse_from_coordinate(axis, angle, self.axis_calibrations)
        self._validate_target(axis, target_pulse)
        self._check(self.api.profile_trap(axis), "GA_PrfTrap(9)")
        self._check(self.api.axis_on(axis), "GA_AxisOn(9)")
        self._check(self.api.clear_status(axis, 1), "GA_ClrSts(9)")
        self._check(self.api.set_trap_prm_single(axis, 1.0, 1.0, 0.0, 0), "GA_SetTrapPrmSingle(9)")
        velocity = AXIS9_MAX_VELOCITY if speed is None else abs(float(speed))
        self._check(self.api.set_velocity(axis, velocity), "GA_SetVel(9)")
        self._check(self.api.set_position(axis, target_pulse), "GA_SetPos(9)")
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
