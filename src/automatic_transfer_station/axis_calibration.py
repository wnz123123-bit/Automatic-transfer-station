from __future__ import annotations

from dataclasses import dataclass
from math import floor


AXIS9_NUM = 9
AXIS9_DEGREES_PER_PULSE = 0.00024
AXIS9_MIN_DEGREES = -5.0
AXIS9_MAX_DEGREES = 365.0
AXIS9_MAX_VELOCITY = 20.0

LINEAR_SPEED_OPTIONS_UM_S = (1.0, 10.0, 100.0, 1000.0, 5000.0)
Z_SPEED_OPTIONS_UM_S = (0.1, 1.0, 10.0, 100.0, 1000.0, 5000.0)
# MicroscopeZ (axis 3) speed notches, capped at 4000 um/s (= 4 mm/s). The focus
# stepper moves smoothly up to 4000 but STALLS at 5000 um/s (= 250 pulse/ms =
# 250 kHz step rate at this axis's 50000 pulse/mm): the motor loses steps while
# the commanded-position readout keeps advancing, so it physically stops mid-hold
# and only nudges on release (the decel ramp passing back through a followable
# speed). 5000 is therefore intentionally NOT offered. (These options drive the GUI
# MicroscopeZ buttons; the gamepad LT/RT triggers use a SEPARATE fixed speed —
# GAMEPAD_MICROSCOPE_Z_SPEED_UM_S below — decoupled from this dropdown.)
MICROSCOPE_Z_SPEED_OPTIONS_UM_S = (1.0, 10.0, 100.0, 1000.0, 2000.0, 3000.0, 4000.0)
# Fixed gamepad speed for the MicroscopeZ triggers (LT/RT). The operator wanted the
# gamepad focus-Z control to ALWAYS top out at 4 mm/s (the stepper's reliable max),
# independent of the GUI MicroscopeZ speed dropdown — so the trigger scales 0..4000
# um/s proportionally regardless of what the on-screen setting shows. Equal to the
# MicroscopeZ speed cap (max of MICROSCOPE_Z_SPEED_OPTIONS_UM_S).
GAMEPAD_MICROSCOPE_Z_SPEED_UM_S = 4000.0
ANGULAR_SPEED_OPTIONS_DEG_S = (0.1, 0.3, 1.0, 2.0, 3.0)
DEFAULT_LINEAR_SPEED_UM_S = 1000.0
DEFAULT_ANGULAR_SPEED_DEG_S = 1.0
AXIS9_TWIST_SPEED_DEG_S = 0.1
MICROSCOPE_Z_PULSES_PER_MM = 50000.0

# Conservative sanity cap on a MicroscopeZ (axis 3) target pulse. Axis 3 has NO
# documented hardware travel (velocity-jog is intentionally unbounded), so this is
# NOT a tight travel limit — it is a deliberately generous bound (1000 mm of travel
# equivalent) used only to reject corrupted/absurd PERSISTED or explicit pulse values
# (e.g. a hand-edited point-store file). 1000 mm is far larger than any real microscope
# focus-Z travel (mm–cm scale), so it can never reject a genuinely marked encoder
# reading; yet it catches absurd values (1e9+) and sits far below the int32 limit
# (2^31 ≈ 4.3e7 vs 2.1e9), so it also forecloses the silent c_long wraparound in
# GA_SetPos. Refine when the real Z travel is known.
MICROSCOPE_Z_SANITY_TRAVEL_MM = 1000.0
MICROSCOPE_Z_MAX_ABS_PULSE = MICROSCOPE_Z_PULSES_PER_MM * MICROSCOPE_Z_SANITY_TRAVEL_MM

LIMIT_TYPE_POSITIVE = 0
LIMIT_TYPE_NEGATIVE = 1

X2_X3_INTERFERENCE_BOUNDARY_PULSE = -851567.0
# Safety gap added on top of the interference boundary (~one 100 ms gamepad
# update of mutual approach at a moderate speed).  Must be > 0.
X2_X3_INTERFERENCE_MARGIN_PULSE = 10000.0


@dataclass(frozen=True)
class AxisCalibration:
    axis: int
    label: str
    unit: str | None
    pulses_per_unit: float | None
    zero_pulse: float | None
    zero_coordinate: float = 0.0
    pulse_sign: float = 1.0
    min_coordinate: float | None = None
    max_coordinate: float | None = None

    def coordinate_from_pulse(self, pulse: float) -> float | None:
        if self.unit is None or self.pulses_per_unit is None or self.zero_pulse is None:
            return None
        return self.zero_coordinate + self.pulse_sign * (float(pulse) - self.zero_pulse) / self.pulses_per_unit

    def pulse_from_coordinate(self, coordinate: float) -> int:
        if self.unit is None or self.pulses_per_unit is None or self.zero_pulse is None:
            raise ValueError(f"axis {self.axis} does not have an absolute calibrated coordinate")
        pulse = self.zero_pulse + (float(coordinate) - self.zero_coordinate) * self.pulses_per_unit / self.pulse_sign
        return _round_half_up(pulse)

    def soft_limit_endpoint_pulse(self, coordinate: float) -> int:
        """Pulse for a soft-limit ENDPOINT, guaranteed inside ``[min, max]``.

        ``pulse_from_coordinate`` rounds half-up, so the rounded endpoint pulse
        can map BACK (via ``coordinate_from_pulse``) up to half a pulse OUTSIDE
        the soft range -- e.g. the rounded ``min_coordinate`` pulse mapping to a
        coordinate just below ``min_coordinate``. That makes the axis's own legal
        travel endpoint read as out-of-range and silently disables a jog
        direction. This rounds the endpoint pulse DIRECTIONALLY: it nudges the
        pulse by +/-1 (whichever moves the mapped coordinate toward the inside of
        the range, accounting for an inverted ``pulse_sign``) until the mapped
        coordinate lands inside ``[min, max]``. The nudge is at most one step
        because half-up rounding never lands more than half a pulse outside, so
        the soft limit is never loosened and the target is provably in range.
        """
        pulse = self.pulse_from_coordinate(coordinate)
        if self.min_coordinate is None or self.max_coordinate is None:
            return pulse
        for _ in range(2):  # at most one correction is ever needed
            mapped = self.coordinate_from_pulse(pulse)
            if mapped is None:
                return pulse
            if mapped < self.min_coordinate:
                # Need a larger coordinate: step pulse the way that raises it.
                pulse += 1 if self.pulse_sign > 0 else -1
            elif mapped > self.max_coordinate:
                # Need a smaller coordinate: step pulse the way that lowers it.
                pulse += -1 if self.pulse_sign > 0 else 1
            else:
                break
        return pulse

    def is_pulse_in_range(self, pulse: float) -> bool:
        coordinate = self.coordinate_from_pulse(pulse)
        if coordinate is None or self.min_coordinate is None or self.max_coordinate is None:
            return True
        return self.min_coordinate <= coordinate <= self.max_coordinate

    def has_absolute_calibration(self) -> bool:
        """True when this axis maps pulses to/from an absolute coordinate."""
        return self.unit is not None and self.pulses_per_unit is not None and self.zero_pulse is not None

    def has_finite_soft_range(self) -> bool:
        """True when this axis has a finite, bounded soft-travel range.

        This is exactly the condition under which ``safe_continuous_target`` is
        able to compute a clamped target instead of returning ``None`` for "no
        absolute range". Axes that lack absolute calibration or soft limits
        (e.g. axis 3, MicroscopeZ) return ``False`` and keep the velocity-jog
        fallback path.
        """
        return (
            self.has_absolute_calibration()
            and self.min_coordinate is not None
            and self.max_coordinate is not None
        )


@dataclass(frozen=True)
class HardLimitConfig:
    positive_io: int | None = None
    negative_io: int | None = None

    @property
    def has_any_limit(self) -> bool:
        return self.positive_io is not None or self.negative_io is not None


@dataclass(frozen=True)
class SafetyCheck:
    ok: bool
    message: str = ""
    axes: tuple[int, ...] = ()


AXIS_CALIBRATIONS = {
    1: AxisCalibration(1, "X1", "mm", 12500.0, 0.0, zero_coordinate=50.0, min_coordinate=0.0, max_coordinate=100.0),
    2: AxisCalibration(2, "Y1", "mm", 12500.0, 0.0, zero_coordinate=25.0, min_coordinate=0.0, max_coordinate=50.0),
    3: AxisCalibration(3, "MicroscopeZ", None, None, None),
    4: AxisCalibration(4, "Z", "mm", 50000.0, -168494.0, min_coordinate=0.0, max_coordinate=9.95),
    5: AxisCalibration(5, "X3", "mm", 50000.0, 687898.0, pulse_sign=-1.0, min_coordinate=-12.5, max_coordinate=12.5),
    # Y3 (axis 6): pulse_sign=-1 — the Y3 motor runs OPPOSITE to the software's
    # assumed +pulse direction (real-hardware finding 2026-06-22: GUI and gamepad
    # "前/后" were reversed). Flipping pulse_sign reverses both the jog direction and
    # the coordinate readout so they agree with the physical stage. Soft range is
    # symmetric (±13 mm) so the bound pulses are unchanged.
    6: AxisCalibration(6, "Y3", "mm", 50000.0, -250000.0, pulse_sign=-1.0, min_coordinate=-13.0, max_coordinate=13.0),
    # X2 negative soft limit matches its documented physical NEG_HARD_LIMIT at
    # -1133407 pulse = -25.30394 mm (its resting/contact position). Using -25.30
    # left the rest position outside the soft range, so every safety scan flagged
    # X2-at-rest as unsafe and tried to stop it. The value is the exact coordinate
    # of the rest pulse so is_pulse_in_range(-1133407) is inclusive-true.
    7: AxisCalibration(7, "X2", "mm", 50000.0, 131790.0, min_coordinate=-25.30394, max_coordinate=25.50),
    # Y2 (axis 8): pulse_sign=-1 — same reversed-direction finding as Y3. Flipping
    # pulse_sign mirrors the coordinate frame, so the asymmetric soft limits are
    # swapped+negated (old min=-25.099/max=25.280 → min=-25.280/max=25.099) to keep
    # the EXACT same physical pulse envelope ([-1339950, 1179000]); the bound
    # mechanical endpoints do not move, only their coordinate labels flip sign.
    8: AxisCalibration(8, "Y2", "mm", 50000.0, -85000.0, pulse_sign=-1.0, min_coordinate=-25.280, max_coordinate=25.099),
    9: AxisCalibration(
        9,
        "R",
        "deg",
        1.0 / AXIS9_DEGREES_PER_PULSE,
        0.0,
        min_coordinate=AXIS9_MIN_DEGREES,
        max_coordinate=AXIS9_MAX_DEGREES,
    ),
}

AXIS_LABELS = {axis: calibration.label for axis, calibration in AXIS_CALIBRATIONS.items()}


def _calibration_for(axis: int, axis_calibrations: dict[int, AxisCalibration] | None = None) -> AxisCalibration:
    return (axis_calibrations or AXIS_CALIBRATIONS)[int(axis)]


def _linear_pulses_per_mm(axis: int, axis_calibrations: dict[int, AxisCalibration] | None = None) -> float:
    calibration = _calibration_for(axis, axis_calibrations)
    pulses_per_mm = calibration.pulses_per_unit
    if pulses_per_mm is None:
        if axis == 3:
            pulses_per_mm = MICROSCOPE_Z_PULSES_PER_MM
        else:
            raise ValueError(f"axis {axis} does not have a linear speed calibration")
    return pulses_per_mm


def _round_half_up(value: float) -> int:
    sign = -1 if value < 0 else 1
    return sign * int(floor(abs(value) + 0.5))


def speed_to_pulse_per_ms(axis: int, speed: float) -> float:
    """Convert GUI physical speed to the controller velocity unit."""
    if axis == AXIS9_NUM:
        return float(speed) / AXIS9_DEGREES_PER_PULSE / 1000.0

    pulses_per_mm = _linear_pulses_per_mm(axis)
    return float(speed) * pulses_per_mm / 1_000_000.0


def step_to_pulse_delta(axis: int, step: float) -> int:
    """Convert GUI physical step to a pulse delta magnitude."""
    step = float(step)
    if axis == AXIS9_NUM:
        pulse_delta = _round_half_up(step / AXIS9_DEGREES_PER_PULSE)
        if step != 0 and pulse_delta == 0:
            return 1 if step > 0 else -1
        return pulse_delta

    pulses_per_mm = _linear_pulses_per_mm(axis)
    pulse_delta = _round_half_up(step * pulses_per_mm / 1000.0)
    if step != 0 and pulse_delta == 0:
        return 1 if step > 0 else -1
    return pulse_delta


def speed_options_for_axis(axis: int) -> tuple[float, ...]:
    if axis == AXIS9_NUM:
        return ANGULAR_SPEED_OPTIONS_DEG_S
    if int(axis) == 3:
        return MICROSCOPE_Z_SPEED_OPTIONS_UM_S
    if int(axis) == 4:
        return Z_SPEED_OPTIONS_UM_S
    return LINEAR_SPEED_OPTIONS_UM_S


def default_speed_for_axis(axis: int) -> float:
    if axis == AXIS9_NUM:
        return DEFAULT_ANGULAR_SPEED_DEG_S
    return DEFAULT_LINEAR_SPEED_UM_S


# Discrete step "notches" the gamepad selector cycles through per axis (GUI step
# spin box stays free-entry; the gamepad only snaps to these). Units match each
# axis's GUI step unit (linear um, Z um, R deg).
STEP_SNAP = {
    # X1 (axis 1) and Y1 (axis 2) became gamepad-selectable in the 2026-06-22 D-pad
    # remap; without a STEP_SNAP entry _gamepad_set_step would raise KeyError. Same
    # linear notches (µm) as the other translation axes.
    1: (1.0, 5.0, 10.0, 50.0, 100.0, 500.0, 1000.0),
    2: (1.0, 5.0, 10.0, 50.0, 100.0, 500.0, 1000.0),
    5: (1.0, 5.0, 10.0, 50.0, 100.0, 500.0, 1000.0),
    6: (1.0, 5.0, 10.0, 50.0, 100.0, 500.0, 1000.0),
    7: (1.0, 5.0, 10.0, 50.0, 100.0, 500.0, 1000.0),
    8: (1.0, 5.0, 10.0, 50.0, 100.0, 500.0, 1000.0),
    9: (0.05, 0.1, 0.5, 1.0, 5.0, 10.0),
    3: (1.0, 5.0, 10.0, 50.0, 100.0, 500.0, 1000.0),
    4: (0.1, 1.0, 5.0, 10.0, 50.0, 100.0, 500.0),
}

# Device-PHOTO hotspot anchor positions, as (x_pct, y_pct) of the overview image
# (0-100; y increases DOWNWARD, image-pixel convention). Used ONLY by
# MainWindow._build_stage_map to place the hotspots on the photo. The gamepad's 2D
# face-button selector does NOT use these — it navigates by SELECTOR_GRID_POSITIONS
# (the GUI card grid, below), intentionally decoupled (2026-06-22) so selection
# follows the on-screen card list rather than the scattered photo geometry.
STAGE_MAP_AXIS_POSITIONS = {
    1: (54, 84),   # X1  整体左右
    2: (22, 76),   # Y1  整体前后
    3: (45, 33),   # MicroscopeZ  显微镜上下 (NOT gamepad-selectable; triggers only)
    4: (72, 50),   # Z   载玻片升降
    5: (24, 66),   # X3  吸附台左右
    6: (34, 58),   # Y3  吸附台前后
    7: (69, 60),   # X2  载玻片左右
    8: (56, 55),   # Y2  载玻片前后
    9: (38, 48),   # R   样品旋转
}

# Grid (col, row) of each gamepad-SELECTABLE axis, matching the GUI axis-card list
# layout (NOT the device photo): left column 吸附台 = X3/Y3/R, right column 载玻片侧
# = X2/Y2/Z, and the 整体位移 row X1/Y1 below. The 2D face-button selector navigates
# by THIS grid (up/down within a column, left/right between columns) so its
# direction feel matches how the operator reads the on-screen card list. row/col
# use the image convention (col=x increasing right, row=y increasing DOWN).
#   col:   0 (吸附台)   1 (载玻片侧)
#   row 0:  X3 (5)       X2 (7)
#   row 1:  Y3 (6)       Y2 (8)
#   row 2:  R  (9)       Z  (4)
#   row 3:  X1 (1)       Y1 (2)   ← 整体位移
SELECTOR_GRID_POSITIONS = {
    5: (0, 0), 6: (0, 1), 9: (0, 2), 1: (0, 3),
    7: (1, 0), 8: (1, 1), 4: (1, 2), 2: (1, 3),
}


def snap_step(axis: int, current: float, delta: int) -> float:
    snaps = STEP_SNAP[int(axis)]
    current = float(current)
    if delta > 0:
        candidates = [s for s in snaps if s > current]
        return candidates[0] if candidates else snaps[-1]
    if delta < 0:
        candidates = [s for s in snaps if s < current]
        return candidates[-1] if candidates else snaps[0]
    nearest = min(range(len(snaps)), key=lambda i: abs(snaps[i] - current))
    return snaps[nearest]


AXIS_HARD_LIMITS = {
    1: HardLimitConfig(positive_io=1, negative_io=0),
    # Y1 positive/forward limit switch is known broken; keep only the confirmed negative IO2.
    2: HardLimitConfig(negative_io=2),
    3: HardLimitConfig(),
    4: HardLimitConfig(positive_io=5, negative_io=4),
    5: HardLimitConfig(positive_io=6, negative_io=7),
    6: HardLimitConfig(positive_io=14, negative_io=9),
    7: HardLimitConfig(positive_io=11, negative_io=10),
    8: HardLimitConfig(positive_io=13, negative_io=12),
    9: HardLimitConfig(),
}


def coordinate_from_pulse(
    axis: int,
    pulse: float,
    axis_calibrations: dict[int, AxisCalibration] | None = None,
) -> float | None:
    return _calibration_for(int(axis), axis_calibrations).coordinate_from_pulse(pulse)


def pulse_from_coordinate(
    axis: int,
    coordinate: float,
    axis_calibrations: dict[int, AxisCalibration] | None = None,
) -> int:
    return _calibration_for(int(axis), axis_calibrations).pulse_from_coordinate(coordinate)


def axis_unit(axis: int) -> str | None:
    return AXIS_CALIBRATIONS[int(axis)].unit


def axis_has_finite_soft_range(
    axis: int,
    axis_calibrations: dict[int, AxisCalibration] | None = None,
) -> bool:
    """True when the axis is "bounded": it has a finite soft-travel range.

    Shares the same calibration source as ``safe_continuous_target`` so the two
    cannot drift: an axis is bounded iff ``safe_continuous_target`` is able to
    return a clamped target rather than ``None`` for "no absolute range".
    """
    return _calibration_for(int(axis), axis_calibrations).has_finite_soft_range()


def axis_has_absolute_calibration(
    axis: int,
    axis_calibrations: dict[int, AxisCalibration] | None = None,
) -> bool:
    """True when the axis maps coordinates to/from pulses (absolute calibration)."""
    return _calibration_for(int(axis), axis_calibrations).has_absolute_calibration()


def is_x2_x3_interference_safe(axis5_pulse: float, axis7_pulse: float) -> bool:
    return (
        float(axis5_pulse) + float(axis7_pulse)
        > X2_X3_INTERFERENCE_BOUNDARY_PULSE + X2_X3_INTERFERENCE_MARGIN_PULSE
    )


def is_target_safe(
    axis: int,
    target_pulse: float,
    current_positions: dict[int, float],
    axis_calibrations: dict[int, AxisCalibration] | None = None,
) -> SafetyCheck:
    axis = int(axis)
    calibration = _calibration_for(axis, axis_calibrations)
    coordinate = calibration.coordinate_from_pulse(target_pulse)
    if not calibration.is_pulse_in_range(target_pulse):
        return SafetyCheck(
            False,
            (
                f"{calibration.label} target {coordinate:.4f} {calibration.unit} is outside "
                f"{calibration.min_coordinate:g}..{calibration.max_coordinate:g} {calibration.unit}"
            ),
            (axis,),
        )

    positions = {int(key): float(value) for key, value in current_positions.items()}
    positions[axis] = float(target_pulse)
    # CP5 fail-safe: when the moving axis IS one of the X2/X3 interference pair
    # (5 or 7) the partner position is REQUIRED to evaluate interference. If it is
    # absent we cannot guarantee the move is safe, so we refuse rather than silently
    # skipping the check (old behaviour: missing partner → treated as safe). Only
    # axes 5 and 7 are in the interference pair; other axes skip the check entirely.
    if axis in (5, 7):
        partner = 7 if axis == 5 else 5
        if partner not in positions:
            return SafetyCheck(
                False,
                f"干涉伙伴位置不可用 (axes (5, 7))",
                (5, 7),
            )
        if not is_x2_x3_interference_safe(positions[5], positions[7]):
            return SafetyCheck(
                False,
                (
                    "X2/X3 interference boundary would be crossed: "
                    f"axis7 + axis5 = {positions[7] + positions[5]:.0f} pulse"
                ),
                (5, 7),
            )

    return SafetyCheck(True)


def unsafe_axes_for_positions(
    positions: dict[int, float],
    axis_calibrations: dict[int, AxisCalibration] | None = None,
) -> SafetyCheck:
    normalized = {int(axis): float(pulse) for axis, pulse in positions.items()}
    bad_axes: list[int] = []
    messages: list[str] = []

    for axis, pulse in normalized.items():
        calibration = (axis_calibrations or AXIS_CALIBRATIONS).get(axis)
        if calibration is None or calibration.is_pulse_in_range(pulse):
            continue
        coordinate = calibration.coordinate_from_pulse(pulse)
        bad_axes.append(axis)
        messages.append(
            f"{calibration.label} is at {coordinate:.4f} {calibration.unit}, outside "
            f"{calibration.min_coordinate:g}..{calibration.max_coordinate:g} {calibration.unit}"
        )

    # CP5 fail-safe: when the snapshot contains ONLY ONE of the X2/X3 pair the
    # interference check cannot be completed — treat the pair as UNSAFE rather than
    # silently skipping (old behaviour). Both absent → no interference relevant,
    # skip. Both present → normal boundary check.
    axis5_present = 5 in normalized
    axis7_present = 7 in normalized
    if axis5_present and axis7_present:
        if not is_x2_x3_interference_safe(normalized[5], normalized[7]):
            bad_axes.extend([5, 7])
            messages.append(
                f"X2/X3 interference boundary reached: axis7 + axis5 = {normalized[7] + normalized[5]:.0f} pulse"
            )
    elif axis5_present or axis7_present:
        # Exactly one partner in the snapshot — cannot evaluate interference, refuse.
        bad_axes.extend([5, 7])
        messages.append("干涉伙伴位置不可用 (axes (5, 7))")

    if bad_axes:
        return SafetyCheck(False, "; ".join(messages), tuple(sorted(set(bad_axes))))
    return SafetyCheck(True)


def safe_continuous_target(
    axis: int,
    direction: int,
    current_positions: dict[int, float],
    axis_calibrations: dict[int, AxisCalibration] | None = None,
    partner_lookahead_pulse: float = 0.0,
) -> int | None:
    """Return the farthest safe pulse target for a bounded continuous move.

    The direction is controller pulse direction, not displayed coordinate sign.
    Axes without absolute range return None and should keep the normal jog path.
    """
    axis = int(axis)
    direction = 1 if direction > 0 else -1
    calibration = _calibration_for(axis, axis_calibrations)
    if not calibration.has_finite_soft_range():
        return None

    # Use directional endpoint rounding so the constructed travel-end target is
    # GUARANTEED inside the soft range. The plain half-up rounding in
    # pulse_from_coordinate can land the endpoint pulse up to half a pulse OUTSIDE
    # [min, max] once a runtime re-zero shifts zero_coordinate to a fractional
    # value, which makes the final is_pulse_in_range gate reject the axis's own
    # legal endpoint and silently disable a whole jog direction (see
    # soft_limit_endpoint_pulse).
    min_pulse = calibration.soft_limit_endpoint_pulse(calibration.min_coordinate)
    max_pulse = calibration.soft_limit_endpoint_pulse(calibration.max_coordinate)
    lower = min(min_pulse, max_pulse)
    upper = max(min_pulse, max_pulse)
    target = upper if direction > 0 else lower

    positions = {int(key): float(value) for key, value in current_positions.items()}
    threshold = X2_X3_INTERFERENCE_BOUNDARY_PULSE + X2_X3_INTERFERENCE_MARGIN_PULSE
    lookahead = abs(float(partner_lookahead_pulse))
    clamped = False
    # Fold partner_lookahead_pulse into the partner term: assume the partner has
    # already moved lookahead pulses toward the boundary before this axis stops.
    if axis == 5 and 7 in positions and target + (positions[7] - lookahead) <= threshold:
        target = _round_half_up(threshold - (positions[7] - lookahead) + 1)
        clamped = True
    elif axis == 7 and 5 in positions and target + (positions[5] - lookahead) <= threshold:
        target = _round_half_up(threshold - (positions[5] - lookahead) + 1)
        clamped = True

    # The interference clamp above is purely boundary-derived; when the pair is
    # already inside the interference zone it can land on the opposite side of the
    # current position, which would turn a requested negative jog into a positive
    # move (and vice versa). Never return a target that runs against the requested
    # direction: refuse instead, so the caller stops rather than reversing. If the
    # moving axis's own position is unknown we cannot verify direction, so refuse
    # whenever the clamp changed the target (an un-clamped upper/lower target is
    # correct by construction and stays allowed).
    current = positions.get(axis)
    if current is None:
        if clamped:
            return None
    elif (direction > 0 and target <= current) or (direction < 0 and target >= current):
        return None

    check = is_target_safe(axis, target, positions, axis_calibrations)
    if not check.ok:
        return None
    return int(target)
